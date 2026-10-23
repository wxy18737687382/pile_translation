import re
from typing import List
from pipeline.models import DocumentBlock, BlockType, ChunkConfig, ChunkGranularity


class Chunker:
    """
    Aggregates or splits a sequence of DocumentBlocks according to configured granularity and length constraints:
    1. SENTENCE: Sentence-level splitting (sentence-by-sentence bilingual alignment)
    2. PARAGRAPH: Default paragraph-level (preserves native layout)
    3. DYNAMIC_WINDOW: Adjustable length window (smart merge of short blocks or split of oversized blocks via min_length / max_length)
    """

    def __init__(self, config: ChunkConfig = None):
        self.config = config or ChunkConfig()
        # Regex for English sentence boundaries without erroneously breaking on common academic abbreviations
        self.abbr_pattern = re.compile(r"\b(e\.g\.|i\.e\.|et al\.|Fig\.|Eq\.|Ref\.|vs\.|Dr\.|Prof\.)\s*$", re.IGNORECASE)

    def split_into_sentences(self, text: str) -> List[str]:
        """Splits text into sentences while avoiding incorrect splitting on common academic abbreviations"""
        # Initial punctuation split: period, question mark, or exclamation mark followed by whitespace and uppercase/digit
        raw_splits = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'‘“])", text.strip())
        sentences: List[str] = []
        buffer = ""

        for part in raw_splits:
            if not part:
                continue
            if buffer:
                # Check if buffer ends with a known abbreviation
                if self.abbr_pattern.search(buffer):
                    buffer += " " + part
                    continue
                else:
                    sentences.append(buffer)
                    buffer = part
            else:
                buffer = part

        if buffer:
            sentences.append(buffer)

        return sentences if sentences else [text]

    def process(self, blocks: List[DocumentBlock]) -> List[DocumentBlock]:
        """Processes all document blocks based on configured granularity"""
        if self.config.granularity == ChunkGranularity.PARAGRAPH:
            return blocks

        if self.config.granularity == ChunkGranularity.SENTENCE:
            return self._process_sentence_level(blocks)

        if self.config.granularity == ChunkGranularity.DYNAMIC_WINDOW:
            return self._process_dynamic_window(blocks)

        return blocks

    def _process_sentence_level(self, blocks: List[DocumentBlock]) -> List[DocumentBlock]:
        """Refines paragraph blocks into sentence-level blocks; structural blocks (headings, math, tables, code) remain intact"""
        result: List[DocumentBlock] = []

        for block in blocks:
            # Only regular paragraphs and list items are split into sentences; headings and structure blocks remain independent
            if block.type in (BlockType.PARAGRAPH, BlockType.LIST_ITEM):
                sentences = self.split_into_sentences(block.original_text)
                if len(sentences) <= 1:
                    result.append(block)
                else:
                    for idx, s in enumerate(sentences):
                        sub_block = DocumentBlock(
                            id=f"{block.id}_s{idx}",
                            type=block.type,
                            original_text=s,
                            level=block.level,
                            metadata={**block.metadata, "sentence_index": idx, "parent_id": block.id}
                        )
                        result.append(sub_block)
            else:
                result.append(block)

        return result

    def _process_dynamic_window(self, blocks: List[DocumentBlock]) -> List[DocumentBlock]:
        """
        Dynamically adjusts block lengths:
        - Structural blocks (headings, math, tables) always remain independent.
        - Short adjacent paragraphs (under same section) are merged towards target_length.
        - Long paragraphs exceeding max_length are safely split along sentence boundaries.
        """
        result: List[DocumentBlock] = []
        current_section = "root"
        merge_buffer: List[DocumentBlock] = []
        buffer_length = 0

        def flush_buffer():
            nonlocal merge_buffer, buffer_length
            if not merge_buffer:
                return
            if len(merge_buffer) == 1:
                result.append(merge_buffer[0])
            else:
                # Merge multiple short paragraphs
                combined_text = "\n\n".join(b.original_text for b in merge_buffer)
                merged_block = DocumentBlock(
                    id=f"{merge_buffer[0].id}_merged_{len(merge_buffer)}",
                    type=BlockType.PARAGRAPH,
                    original_text=combined_text,
                    metadata={
                        "merged_count": len(merge_buffer),
                        "original_ids": [b.id for b in merge_buffer],
                        "section": current_section
                    }
                )
                result.append(merged_block)
            merge_buffer = []
            buffer_length = 0

        for block in blocks:
            # When encountering a heading or special structural block, flush current buffer first
            if block.type in (BlockType.TITLE, BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3):
                flush_buffer()
                current_section = block.original_text
                result.append(block)
                continue

            if block.type in (
                BlockType.MATH_BLOCK,
                BlockType.TABLE,
                BlockType.CODE_BLOCK,
                BlockType.DIVIDER,
                BlockType.IMAGE,
                BlockType.IMAGE_CAPTION,
                BlockType.FOOTNOTE,
                BlockType.REFERENCE,
                BlockType.AUTHOR_INFO,
                BlockType.RAW,
            ):
                flush_buffer()
                result.append(block)
                continue

            # Regular paragraph handling
            text_len = len(block.original_text)

            # Oversized paragraph: flush buffer first, then split along sentence boundaries
            if text_len > self.config.max_length:
                flush_buffer()
                sentences = self.split_into_sentences(block.original_text)
                sub_chunk = ""
                chunk_idx = 0
                for s in sentences:
                    if len(sub_chunk) + len(s) > self.config.max_length and sub_chunk:
                        result.append(DocumentBlock(
                            id=f"{block.id}_p{chunk_idx}",
                            type=BlockType.PARAGRAPH,
                            original_text=sub_chunk.strip(),
                            metadata={**block.metadata, "split_index": chunk_idx}
                        ))
                        chunk_idx += 1
                        sub_chunk = s
                    else:
                        sub_chunk = (sub_chunk + " " + s).strip()
                if sub_chunk:
                    result.append(DocumentBlock(
                        id=f"{block.id}_p{chunk_idx}",
                        type=BlockType.PARAGRAPH,
                        original_text=sub_chunk.strip(),
                        metadata={**block.metadata, "split_index": chunk_idx}
                    ))
                continue

            # Check if block can be merged into current buffer
            if buffer_length + text_len <= self.config.target_length:
                merge_buffer.append(block)
                buffer_length += text_len
                # Flush if min_length and target_length are reached
                if buffer_length >= self.config.min_length and buffer_length >= self.config.target_length:
                    flush_buffer()
            else:
                # Flush existing buffer and start new buffer
                flush_buffer()
                merge_buffer.append(block)
                buffer_length = text_len

        flush_buffer()
        return result

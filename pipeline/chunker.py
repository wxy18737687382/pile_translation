import re
from typing import List
from pipeline.models import DocumentBlock, BlockType, ChunkConfig, ChunkGranularity


class Chunker:
    """
    负责将 DocumentBlock 序列按照设定的粒度与长度要求进行聚合或切分：
    1. SENTENCE: 句子级切分（逐句对照）
    2. PARAGRAPH: 默认段落级（保持原生排版）
    3. DYNAMIC_WINDOW: 可调节长度窗口（按 min_length / max_length 智能合并碎片段落或拆分超长段落）
    """

    def __init__(self, config: ChunkConfig = None):
        self.config = config or ChunkConfig()
        # 英文句子边界断句正则（考虑常见缩写如 e.g., i.e., et al., Fig. 等不误切）
        self.abbr_pattern = re.compile(r"\b(e\.g\.|i\.e\.|et al\.|Fig\.|Eq\.|Ref\.|vs\.|Dr\.|Prof\.)\s*$", re.IGNORECASE)

    def split_into_sentences(self, text: str) -> List[str]:
        """按句子切分文本，同时避免在学术常见缩写处错误断句"""
        # 初步切分标点：句号、问号、感叹号后跟空格与大写字符/数字
        raw_splits = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'‘“])", text.strip())
        sentences: List[str] = []
        buffer = ""

        for part in raw_splits:
            if not part:
                continue
            if buffer:
                # 检查 buffer 结尾是否为已知缩写
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
        """根据配置的粒度处理所有块"""
        if self.config.granularity == ChunkGranularity.PARAGRAPH:
            return blocks

        if self.config.granularity == ChunkGranularity.SENTENCE:
            return self._process_sentence_level(blocks)

        if self.config.granularity == ChunkGranularity.DYNAMIC_WINDOW:
            return self._process_dynamic_window(blocks)

        return blocks

    def _process_sentence_level(self, blocks: List[DocumentBlock]) -> List[DocumentBlock]:
        """将段落块细化为句子级块，非段落块（标题、公式、表格、代码）保持原样"""
        result: List[DocumentBlock] = []

        for block in blocks:
            # 只有普通段落或列表项进行逐句拆分，标题和结构块保持独立
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
        动态调节长度：
        - 标题、公式、表格等结构块始终保持独立。
        - 过短的相邻段落（在同一小节下）合并至接近 target_length 或超过 min_length。
        - 超过 max_length 的长段落按句子边界安全拆分。
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
                # 合并多个短段落
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
            # 遇到标题或特殊结构块，必须先清空当前合并缓冲区
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

            # 普通段落处理
            text_len = len(block.original_text)

            # 超长段落：先清空前面的 buffer，然后对其按句子拆分
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

            # 检查是否可以与当前 buffer 合并
            if buffer_length + text_len <= self.config.target_length:
                merge_buffer.append(block)
                buffer_length += text_len
                # 如果已经达到 min_length 且接近 target，可选择冲刷
                if buffer_length >= self.config.min_length and buffer_length >= self.config.target_length:
                    flush_buffer()
            else:
                # 超过目标长度，将之前的 buffer 冲刷并开始新 buffer
                flush_buffer()
                merge_buffer.append(block)
                buffer_length = text_len

        flush_buffer()
        return result

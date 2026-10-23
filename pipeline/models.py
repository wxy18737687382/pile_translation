from enum import Enum
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field


class BlockType(str, Enum):
    TITLE = "title"
    HEADER_1 = "h1"
    HEADER_2 = "h2"
    HEADER_3 = "h3"
    PARAGRAPH = "paragraph"
    MATH_BLOCK = "math_block"        # Display math block ($$...$$)
    TABLE = "table"                  # Table block
    CODE_BLOCK = "code_block"        # Code block
    LIST_ITEM = "list_item"          # List item
    IMAGE = "image"                  # Extracted standalone figure/photo block
    IMAGE_CAPTION = "image_caption"  # Figure / Table caption
    FOOTNOTE = "footnote"            # Footnote block
    REFERENCE = "reference"          # Reference entry ([1] ...)
    AUTHOR_INFO = "author_info"      # First page author & affiliation info block
    DIVIDER = "divider"
    RAW = "raw"


class ChunkGranularity(str, Enum):
    SENTENCE = "sentence"          # Sentence-level granularity (sentence-by-sentence bilingual alignment)
    PARAGRAPH = "paragraph"        # Paragraph-level granularity (preserves native boundaries and PDF bboxes)
    DYNAMIC_WINDOW = "dynamic"     # Dynamic adjustable window (smart merge/split by token/character length)


class ChunkConfig(BaseModel):
    granularity: ChunkGranularity = ChunkGranularity.PARAGRAPH
    # Dynamic length mode parameters
    target_length: int = 300       # Target combined character/word count
    min_length: int = 150          # Minimum chunk threshold (merge with adjacent block if shorter)
    max_length: int = 800          # Maximum chunk threshold (safely truncate at punctuation if longer)
    preserve_headers: bool = True  # Headings remain independent and do not participate in cross-block merging
    preserve_tables: bool = True   # Tables remain independent and do not participate in cross-block merging


class DocumentBlock(BaseModel):
    id: str
    type: BlockType
    original_text: str
    translated_text: Optional[str] = None
    level: int = 0                 # Heading level (1, 2, 3) or indentation depth
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Placeholder lookup map: placeholder token (e.g. {{MATH_INLINE_0}}) -> actual content
    placeholders: Dict[str, str] = Field(default_factory=dict)

    # Native PDF coordinates and layout attributes (extracted by MinerU engine for 1:1 overlay alignment)
    page_num: Optional[int] = None             # 0-indexed page number
    bbox: Optional[List[float]] = None         # [x0, y0, x1, y1] absolute page coordinates (pt)
    page_width: Optional[float] = None         # Page width (pt)
    page_height: Optional[float] = None        # Page height (pt)
    font_size: Optional[float] = None          # Original font size (pt)
    is_bold: bool = False                      # Whether the text is bold
    align: str = "left"                        # Text alignment: left | center | justify
    image_path: Optional[str] = None           # Relative path to image file (e.g. images/page_3_fig_1.png)
    skip_pdf_overlay: bool = False             # Whether to retain original vector graphics in PDF overlay
    footnote_id: Optional[str] = None          # Footnote identifier (e.g. *, dagger, double-dagger, 1, 2)


class TranslationUnit(BaseModel):
    """Core translation unit passed to LLM containing contextual metadata"""
    id: str
    source_text: str
    masked_text: str
    placeholders: Dict[str, str] = Field(default_factory=dict)
    block_type: BlockType = BlockType.PARAGRAPH
    section_context: Optional[str] = None
    translated_text: Optional[str] = None

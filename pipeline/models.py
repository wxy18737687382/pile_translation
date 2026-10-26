from enum import Enum
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field


class BlockType(str, Enum):
    TITLE = "title"
    HEADER_1 = "h1"
    HEADER_2 = "h2"
    HEADER_3 = "h3"
    PARAGRAPH = "paragraph"
    MATH_BLOCK = "math_block"        # 独立公式块 ($$...$$)
    TABLE = "table"                  # 表格块
    CODE_BLOCK = "code_block"        # 代码块
    LIST_ITEM = "list_item"          # 列表项
    IMAGE = "image"                  # 提取出的独立插图/照片块
    IMAGE_CAPTION = "image_caption"  # 图表描述 (Figure / Table Caption)
    FOOTNOTE = "footnote"            # 页脚角注/脚注 (Footnote)
    REFERENCE = "reference"          # 参考文献条目 ([1] ...)
    AUTHOR_INFO = "author_info"      # 首页作者与机构信息块
    DIVIDER = "divider"
    RAW = "raw"


class ChunkGranularity(str, Enum):
    SENTENCE = "sentence"          # 句子级粒度（逐句双语对照）
    PARAGRAPH = "paragraph"        # 自然段落级粒度（保留原生段落边界与PDF坐标框）
    DYNAMIC_WINDOW = "dynamic"     # 动态可调节长度（按字数/字符数区间智能合并与拆分）


class ParagraphFormatMode(str, Enum):
    REORGANIZE = "reorganize"      # 重新整理翻译 (默认): 合并段内各行并消除断词，生成连贯通顺的学术段落
    BY_LINE = "by-line"            # 按原行翻译: 严格保留原版 PDF 中每行的物理边界与换行格式


class ChunkConfig(BaseModel):
    granularity: ChunkGranularity = ChunkGranularity.PARAGRAPH
    para_format: ParagraphFormatMode = ParagraphFormatMode.REORGANIZE
    # 动态长度模式相关参数
    target_length: int = 300       # 目标合并字符/词数
    min_length: int = 150          # 最小分块阈值（过短时与后文同小节同类型块合并）
    max_length: int = 800          # 最大分块阈值（超长时在句号/标点处安全截断）
    preserve_headers: bool = True  # 标题必须独立，不参与跨块合并
    preserve_tables: bool = True   # 表格必须独立，不参与跨块合并


class DocumentBlock(BaseModel):
    id: str
    type: BlockType
    original_text: str
    translated_text: Optional[str] = None
    level: int = 0                 # 标题级别 (1, 2, 3) 或缩进深度
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # 占位符记录表: 占位标识符 (如 {{MATH_INLINE_0}}) -> 真实内容
    placeholders: Dict[str, str] = Field(default_factory=dict)

    # 原版 PDF 坐标与版面属性（由 MinerU 前置引擎提取，用于左右 1:1 原位对照）
    page_num: Optional[int] = None             # 0-indexed 页码
    bbox: Optional[List[float]] = None         # [x0, y0, x1, y1] 页面绝对坐标 (pt)
    page_width: Optional[float] = None         # 所在页宽度 (pt)
    page_height: Optional[float] = None        # 所在页高度 (pt)
    font_size: Optional[float] = None          # 原文字号 (pt)
    is_bold: bool = False                      # 是否粗体
    align: str = "left"                        # 对齐方式: left | center | justify
    image_path: Optional[str] = None           # 插图文件相对路径 (如 images/page_3_fig_1.png)
    skip_pdf_overlay: bool = False             # 是否在右页 PDF 原位保留原矢量内容（如独立公式、图片、作者栏、页码）
    footnote_id: Optional[str] = None          # 角注标识符（如 ∗, †, ‡, 4, 5）
    cross_page_partner_id: Optional[str] = None# 跨页关联块 ID
    is_cross_page_tail: bool = False           # 是否为跨页断开的前半段
    is_cross_page_head: bool = False           # 是否为跨页接续的后半段


class TranslationUnit(BaseModel):
    """送入 LLM 翻译的核心单元，包含上下文信息"""
    id: str
    source_text: str
    masked_text: str
    placeholders: Dict[str, str] = Field(default_factory=dict)
    block_type: BlockType = BlockType.PARAGRAPH
    section_context: Optional[str] = None
    translated_text: Optional[str] = None

import re
from pathlib import Path
from typing import List, Union, Optional
from pipeline.models import DocumentBlock, BlockType
from pipeline.mineru_engine import MinerUEngine


class DocumentParser:
    """
    负责解析输入文档（PDF 或 Markdown/Text）：
    - 针对 PDF 文档：接入 MinerU 作为文档前置处理引擎，先提取全文结构化 Markdown (full.md)、
      原位插图 (images/*.png) 以及带精确页面坐标 (page_num, bbox) 与角注 (<sup>/<sub>) 的 DocumentBlock 列表。
    - 针对 Markdown/Text 文档：直接解析为结构化 DocumentBlock 列表。
    """

    def __init__(
        self,
        mineru_mode: str = "auto",
        mineru_api_key: Optional[str] = None,
        para_format: str = "reorganize"
    ):
        self.para_format = para_format
        self.mineru_engine = MinerUEngine(
            mode=mineru_mode,
            api_key=mineru_api_key,
            para_format=para_format
        )
        self.last_full_markdown: Optional[str] = None
        self.last_mineru_dir: Optional[Path] = None

    def parse(
        self,
        source: Union[str, Path],
        output_dir: Union[str, Path] = "./output"
    ) -> List[DocumentBlock]:
        path = Path(source) if isinstance(source, (str, Path)) and Path(source).exists() else None

        if path:
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                return self.parse_pdf(path, Path(output_dir))
            elif suffix in (".md", ".markdown", ".txt"):
                text = path.read_text(encoding="utf-8")
                self.last_full_markdown = text
                return self.parse_markdown(text)
            else:
                raise ValueError(f"不支持的文件格式: {suffix}，支持 .pdf, .md, .txt")
        else:
            text = str(source)
            self.last_full_markdown = text
            return self.parse_markdown(text)

    def parse_pdf(self, pdf_path: Path, output_dir: Path = Path("./output")) -> List[DocumentBlock]:
        """使用 MinerU 前置处理引擎提取全文 Markdown、插图、角注及精确 PDF 坐标块"""
        output_dir.mkdir(parents=True, exist_ok=True)
        full_md, blocks, mineru_dir = self.mineru_engine.process_pdf(pdf_path, output_dir)
        self.last_full_markdown = full_md
        self.last_mineru_dir = mineru_dir

        # 将前置引擎提取的全文英文 Markdown 保存至输出目录
        source_md_path = output_dir / f"{pdf_path.stem}_source.md"
        source_md_path.write_text(full_md, encoding="utf-8")
        return blocks

    def parse_markdown(self, markdown_text: str) -> List[DocumentBlock]:
        """将 Markdown 文本解析为原子 Block 列表"""
        blocks: List[DocumentBlock] = []
        lines = markdown_text.splitlines()

        i = 0
        n = len(lines)
        block_counter = 0

        while i < n:
            line = lines[i]
            stripped = line.strip()

            # 1. 空行跳过
            if not stripped:
                i += 1
                continue

            # 2. 多行代码块 (```)
            if stripped.startswith("```"):
                code_lines = [line]
                i += 1
                while i < n:
                    code_lines.append(lines[i])
                    if lines[i].strip().startswith("```"):
                        i += 1
                        break
                    i += 1
                blocks.append(DocumentBlock(
                    id=f"b_{block_counter}",
                    type=BlockType.CODE_BLOCK,
                    original_text="\n".join(code_lines)
                ))
                block_counter += 1
                continue

            # 3. 独立公式块 ($$)
            if stripped.startswith("$$"):
                math_lines = [line]
                if stripped.endswith("$$") and len(stripped) > 2:
                    i += 1
                else:
                    i += 1
                    while i < n:
                        math_lines.append(lines[i])
                        if lines[i].strip().endswith("$$"):
                            i += 1
                            break
                        i += 1
                blocks.append(DocumentBlock(
                    id=f"b_{block_counter}",
                    type=BlockType.MATH_BLOCK,
                    original_text="\n".join(math_lines)
                ))
                block_counter += 1
                continue

            # 4. 独立插图行 (![alt](path))
            if re.match(r"^!\[.*?\]\(.*?\)$", stripped):
                m_img = re.match(r"^!\[.*?\]\((.*?)\)$", stripped)
                blocks.append(DocumentBlock(
                    id=f"b_{block_counter}",
                    type=BlockType.IMAGE,
                    original_text=stripped,
                    translated_text=stripped,
                    image_path=m_img.group(1) if m_img else None,
                    skip_pdf_overlay=True
                ))
                block_counter += 1
                i += 1
                continue

            # 5. 表格块 (| ... |)
            if stripped.startswith("|") and stripped.endswith("|"):
                table_lines = [line]
                i += 1
                while i < n and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                    table_lines.append(lines[i])
                    i += 1
                blocks.append(DocumentBlock(
                    id=f"b_{block_counter}",
                    type=BlockType.TABLE,
                    original_text="\n".join(table_lines)
                ))
                block_counter += 1
                continue

            # 6. 分割线 (--- or ***)
            if re.match(r"^(\-{3,}|\*{3,}|_{3,})$", stripped):
                blocks.append(DocumentBlock(
                    id=f"b_{block_counter}",
                    type=BlockType.DIVIDER,
                    original_text=line
                ))
                block_counter += 1
                i += 1
                continue

            # 7. 各级标题 (#, ##, ###)
            header_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if header_match:
                level = len(header_match.group(1))
                b_type = BlockType.HEADER_1 if level == 1 else (BlockType.HEADER_2 if level == 2 else BlockType.HEADER_3)
                blocks.append(DocumentBlock(
                    id=f"b_{block_counter}",
                    type=b_type,
                    original_text=line,
                    level=level
                ))
                block_counter += 1
                i += 1
                continue

            # 8. 列表项 (- item or 1. item)
            list_match = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
            if list_match:
                list_lines = [line]
                i += 1
                while i < n and lines[i].strip() and (lines[i].startswith("    ") or lines[i].startswith("\t")):
                    list_lines.append(lines[i])
                    i += 1
                blocks.append(DocumentBlock(
                    id=f"b_{block_counter}",
                    type=BlockType.LIST_ITEM,
                    original_text="\n".join(list_lines)
                ))
                block_counter += 1
                continue

            # 9. 普通段落
            para_lines = [line]
            i += 1
            while i < n:
                next_stripped = lines[i].strip()
                if not next_stripped:
                    break
                if next_stripped.startswith("```") or next_stripped.startswith("$$") or \
                   (next_stripped.startswith("|") and next_stripped.endswith("|")) or \
                   re.match(r"^(#{1,6})\s+", next_stripped) or \
                   re.match(r"^(\-{3,}|\*{3,})$", next_stripped) or \
                   re.match(r"^(\s*)([-*+]|\d+\.)\s+", lines[i]):
                    break
                para_lines.append(lines[i])
                i += 1

            blocks.append(DocumentBlock(
                id=f"b_{block_counter}",
                type=BlockType.PARAGRAPH,
                original_text=" ".join(para_lines)
            ))
            block_counter += 1

        return blocks

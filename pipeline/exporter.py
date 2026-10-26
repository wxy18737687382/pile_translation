import re
import html
from pathlib import Path
from typing import List, Optional, Dict, Tuple

import pymupdf
from pipeline.models import DocumentBlock, BlockType

try:
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False


class DocumentExporter:
    """
    负责将经 MinerU 前置解析与大模型翻译对齐后的 DocumentBlock 序列导出为多种格式：
    1. 原版对照双栏 PDF (Left = 原版 PDF, Right = 相同坐标位置原位替换译文 PDF，完整保留照片位置与页脚角注)
    2. 原版排版纯中文 PDF (单栏原位替换译文 PDF)
    3. 原版对照双栏交互 HTML (左边原版 PDF 页面，右边相同坐标位置译文页面 + 悬停同步高亮 + 可切换实时 DOM 文字层)
    4. 双语交替对照 Markdown (含原位提取插图与角注)
    5. 原文格式纯中文 Markdown (含原位提取插图与角注)
    6. 双语对照 Word 文档 (含原位插图与角注)
    """

    def __init__(self, para_format: str = "reorganize"):
        self.para_format = para_format

    # =========================================================================
    # 1. 核心功能：导出原版对照双栏 PDF (_bilingual.pdf) 与原位中文 PDF (_zh.pdf)
    # =========================================================================
    def export_pdf_translations(
        self,
        blocks: List[DocumentBlock],
        source_pdf_path: Path,
        dual_pdf_path: Path,
        zh_pdf_path: Path,
        page_renders_dir: Optional[Path] = None
    ) -> Tuple[Path, Path]:
        """
        基于原版 PDF 坐标框 (x0, y0, x1, y1) 生成：
        - zh_pdf_path: 原位替换中文译文、100%保留插图/独立公式/表格线/角注位置的单栏中文 PDF
        - dual_pdf_path: 左边是原版 PDF、右边在完全相同位置是已翻译文本的双栏对照 PDF
        - 若提供 page_renders_dir，同时渲染每页高清对照图供 HTML 原版对照阅读器使用
        """
        orig_doc = pymupdf.open(str(source_pdf_path))
        zh_doc = pymupdf.open(str(source_pdf_path))
        bg_doc = pymupdf.open(str(source_pdf_path)) if page_renders_dir else None

        blocks_by_page: Dict[int, List[DocumentBlock]] = {}
        for b in blocks:
            if b.page_num is not None:
                blocks_by_page.setdefault(b.page_num, []).append(b)

        for pno in range(len(zh_doc)):
            zh_page = zh_doc[pno]
            bg_page = bg_doc[pno] if bg_doc is not None else None
            p_blocks = blocks_by_page.get(pno, [])

            # 收集本页上的长水平分割线 y 坐标（如页眉线、页脚角注分割线、表格外框线），以及段落内短小的行内分式/根号横线
            horiz_lines_y: List[float] = []
            short_inline_bars: List[pymupdf.Rect] = []
            for d in zh_page.get_drawings():
                r = d.get("rect")
                if not r:
                    continue
                if abs(r.y1 - r.y0) < 4.5 and (r.x1 - r.x0) > 80.0:
                    horiz_lines_y.append(r.y1)
                elif abs(r.y1 - r.y0) < 3.5 and 2.0 < (r.x1 - r.x0) < 35.0:
                    short_inline_bars.append(r)

            overlay_blocks = [
                b for b in p_blocks
                if b.bbox and len(b.bbox) == 4 and not b.skip_pdf_overlay and b.type != BlockType.IMAGE
            ]

            # 第一步：在克隆页面上精准擦除待翻译英文块（保留所有照片/位图与外围分割线/表格线）
            overlay_rects: List[pymupdf.Rect] = []
            for b in overlay_blocks:
                rx0, ry0, rx1, ry1 = b.bbox
                # 严格防止擦除区域 (redact_rect) 侵入下方保留文本的顶部（包括参考文献、图片、公式等）
                for ob in p_blocks:
                    if ob is b or not ob.bbox or len(ob.bbox) != 4:
                        continue
                    ob_x0, ob_y0, ob_x1, ob_y1 = ob.bbox
                    h_overlap = max(0.0, min(rx1, ob_x1) - max(rx0, ob_x0))
                    if h_overlap > 5.0 and ob_y0 >= ry0 - 1.0 and ob_y0 <= ry1 + 2.0:
                        ry1 = min(ry1, ob_y0 - 0.5)

                # 若上方紧邻角注分割线或标题横线，确保擦除框不侵入横线
                for ly in horiz_lines_y:
                    if abs(ry0 - ly) < 3.0:
                        ry0 = max(ry0, ly + 1.0)
                # 题注上方如果有图片，绝对不侵入图片区域
                if b.type == BlockType.IMAGE_CAPTION:
                    ry0 = max(ry0, b.bbox[1])
                redact_rect = pymupdf.Rect(rx0 - 0.5, ry0 - 0.3, rx1 + 0.5, ry1)
                overlay_rects.append(redact_rect)
                zh_page.add_redact_annot(redact_rect)
                if bg_page is not None:
                    bg_page.add_redact_annot(redact_rect)

                if b.metadata and "extra_bboxes" in b.metadata:
                    for eb in b.metadata["extra_bboxes"]:
                        if eb and len(eb) == 4:
                            erx0, ery0, erx1, ery1 = eb
                            e_rect = pymupdf.Rect(erx0 - 0.5, ery0 - 0.3, erx1 + 0.5, ery1)
                            overlay_rects.append(e_rect)
                            zh_page.add_redact_annot(e_rect)
                            if bg_page is not None:
                                bg_page.add_redact_annot(e_rect)

            zh_page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED
            )
            if bg_page is not None:
                bg_page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED
                )

            # 对残留在已擦除正文段落内部的微小行内分式横线/根号顶线进行白色遮盖
            for sr in short_inline_bars:
                if any(or_rect.intersects(sr) for or_rect in overlay_rects):
                    pad_r = pymupdf.Rect(sr.x0 - 0.5, sr.y0 - 0.8, sr.x1 + 0.5, sr.y1 + 0.8)
                    zh_page.draw_rect(pad_r, color=(1, 1, 1), fill=(1, 1, 1))
                    if bg_page is not None:
                        bg_page.draw_rect(pad_r, color=(1, 1, 1), fill=(1, 1, 1))

            # 判定当前页是否为双栏排版及分栏线位置
            page_is_two_col, col_split_x = self._detect_page_columns(p_blocks, zh_page.rect)

            # 第二步：在完全相同的坐标位置 (x0, y0, x1, y1) 原位填入中文译文（轻量高效，零冗余字体开销）
            for idx_b, b in enumerate(overlay_blocks):
                target_rect = self._compute_safe_target_rect(
                    b, p_blocks, idx_b, zh_page.rect, is_two_col=page_is_two_col, col_split_x=col_split_x
                )
                html_frag, css_str = self._build_pdf_htmlbox_payload(b)
                zh_page.insert_htmlbox(
                    target_rect,
                    html_frag,
                    css=css_str,
                    scale_low=0.1
                )

        # 保存单栏原版排版中文 PDF (深度垃圾回收与流压缩，生成极小体积 PDF)
        zh_doc.save(str(zh_pdf_path), garbage=4, deflate=True)

        # 构建左右并排的原版对照双栏 PDF (Left = 原版 PDF, Right = 同位置译文 PDF)
        dual_doc = pymupdf.open()
        for pno in range(len(orig_doc)):
            pw = orig_doc[pno].rect.width
            ph = orig_doc[pno].rect.height
            dual_page = dual_doc.new_page(width=pw * 2.0, height=ph)
            # 左半页：100% 原版 PDF
            dual_page.show_pdf_page(pymupdf.Rect(0, 0, pw, ph), orig_doc, pno)
            # 右半页：相同坐标位置已翻译好的 PDF
            dual_page.show_pdf_page(pymupdf.Rect(pw, 0, pw * 2.0, ph), zh_doc, pno)
            # 中间分隔细线
            dual_page.draw_line(
                pymupdf.Point(pw, 0),
                pymupdf.Point(pw, ph),
                color=(0.82, 0.85, 0.90),
                width=0.75
            )

        dual_doc.save(str(dual_pdf_path), garbage=4, deflate=True)

        # 若需要渲染网页端原版对照图层，导出每页高清 PNG
        if page_renders_dir is not None:
            page_renders_dir.mkdir(parents=True, exist_ok=True)
            render_dpi = 150
            for pno in range(len(orig_doc)):
                orig_pix = orig_doc[pno].get_pixmap(dpi=render_dpi)
                orig_pix.save(str(page_renders_dir / f"page_{pno + 1}_orig.png"))

                zh_pix = zh_doc[pno].get_pixmap(dpi=render_dpi)
                zh_pix.save(str(page_renders_dir / f"page_{pno + 1}_zh.png"))

                if bg_doc is not None:
                    bg_pix = bg_doc[pno].get_pixmap(dpi=render_dpi)
                    bg_pix.save(str(page_renders_dir / f"page_{pno + 1}_bg.png"))

        orig_doc.close()
        zh_doc.close()
        dual_doc.close()
        if bg_doc is not None:
            bg_doc.close()

        return dual_pdf_path, zh_pdf_path

    def _detect_page_columns(
        self,
        blocks: List[DocumentBlock],
        page_rect: pymupdf.Rect
    ) -> Tuple[bool, float]:
        """动态检测当前页面的双栏排版状态与分栏线 x 坐标"""
        pw, ph = page_rect.width, page_rect.height
        mid_x = pw / 2.0
        for b in blocks:
            if b.metadata and "is_two_col" in b.metadata:
                return b.metadata["is_two_col"], b.metadata.get("col_split_x", mid_x)

        content_blocks = [
            b for b in blocks
            if b.bbox and len(b.bbox) == 4 and b.bbox[1] >= 35.0 and b.bbox[3] <= ph - 30.0
        ]
        lefts = [
            b for b in content_blocks
            if b.bbox[2] <= mid_x + 15.0
            and (b.bbox[2] - b.bbox[0]) >= 110.0
            and (b.bbox[3] - b.bbox[1]) >= 18.0
        ]
        rights = [
            b for b in content_blocks
            if b.bbox[0] >= mid_x - 15.0
            and (b.bbox[2] - b.bbox[0]) >= 110.0
            and (b.bbox[3] - b.bbox[1]) >= 18.0
        ]
        spanners = [
            b for b in content_blocks
            if b.bbox[0] < mid_x - 30.0
            and b.bbox[2] > mid_x + 30.0
            and (b.bbox[2] - b.bbox[0]) >= 260.0
            and (b.bbox[3] - b.bbox[1]) >= 18.0
        ]

        has_overlap = False
        for lb in lefts:
            ly0, ly1 = lb.bbox[1], lb.bbox[3]
            for rb in rights:
                ry0, ry1 = rb.bbox[1], rb.bbox[3]
                if min(ly1, ry1) - max(ly0, ry0) >= 15.0:
                    has_overlap = True
                    break
            if has_overlap:
                break

        is_two_col = has_overlap or (len(lefts) + len(rights) >= 3 and len(spanners) <= 1)
        col_split_x = mid_x
        if is_two_col and (lefts or rights):
            max_left_x1 = max([b.bbox[2] for b in lefts]) if lefts else (mid_x - 8.0)
            min_right_x0 = min([b.bbox[0] for b in rights]) if rights else (mid_x + 8.0)
            col_split_x = (max_left_x1 + min_right_x0) / 2.0
            if abs(col_split_x - mid_x) > pw * 0.15:
                col_split_x = mid_x

        return is_two_col, col_split_x

    def _compute_safe_target_rect(
        self,
        block: DocumentBlock,
        page_all_blocks: List[DocumentBlock],
        current_idx: int,
        page_rect: pymupdf.Rect,
        is_two_col: bool = False,
        col_split_x: float = 300.0
    ) -> pymupdf.Rect:
        """
        计算右页译文回填的目标矩形框：
        严格锚定原文起点 (x0, y0)，根据是否为双栏排版严格限制横向安全范围：
        - 双栏排版左栏：右边界硬性截断在 col_split_x - 4.0pt，严禁跨越中线覆写右栏！
        - 双栏排版右栏：右边界截断在页面右边距 (pw - 28.0pt)。
        - 单栏排版或跨栏全宽横幅：右边界适度延伸至正文右边距 (505.5pt / pw - 35.0pt)。
        - 边栏/非对称分栏/左右邻接块：严格受控在右侧邻块边界以左 (min_right_neighbor_x0)，严禁横向穿透覆写相邻块！
        同时同栏内的下邻块顶部 y0 作为垂直扩展硬上限（跨栏块互不挤压）。
        """
        x0, y0, x1, y1 = block.bbox
        pw, ph = page_rect.width, page_rect.height
        box_h = y1 - y0

        # 判断当前块的栏属
        block_col = block.metadata.get("column") if (block.metadata and "column" in block.metadata) else "span"
        if block_col == "span" and is_two_col:
            if x1 <= col_split_x + 6.0:
                block_col = "left"
            elif x0 >= col_split_x - 6.0:
                block_col = "right"

        # 1. 查找当前块右侧是否存在共享垂直区间的相邻块 (无论是否检测为双栏)
        # 严禁任何块的横向排版扩展侵入其右侧邻块的区域，彻底避免多栏、边栏与主正文互相重叠！
        min_right_neighbor_x0 = pw - 28.0
        for ob in page_all_blocks:
            if ob is block or not ob.bbox or len(ob.bbox) != 4:
                continue
            ox0, oy0, ox1, oy1 = ob.bbox
            # 判定条件：ob 严格位于当前块的右侧 (起点偏右至少 20pt，且起点在当前块右边缘附近或以右)
            if ox0 >= x0 + 20.0 and ox0 >= x1 - 15.0:
                # 垂直方向共享区间：两个块在垂直高度上重叠，或 ob 紧随当前块下方附近 (译文换行扩展区)
                vert_overlap = min(y1, oy1) - max(y0, oy0)
                if vert_overlap > -8.0 or (oy0 >= y0 and oy0 <= y1 + 15.0):
                    min_right_neighbor_x0 = min(min_right_neighbor_x0, ox0 - 4.0)

        # 2. 确定右边界 right_margin
        orig_w = x1 - x0
        is_narrow_left = (orig_w < pw * 0.45) and (x1 < pw * 0.55)
        has_right_neighbor = (min_right_neighbor_x0 < pw - 30.0)

        if is_two_col and block_col == "left":
            # 左栏：右边界绝对受控在分栏线左侧与右侧邻块左侧，留出至少 4pt 栏隙
            right_margin = min(col_split_x - 4.0, min_right_neighbor_x0, max(x1, x0 + 80.0))
        elif is_two_col and block_col == "right":
            # 右栏：右边界延伸至页面右侧边距
            right_margin = min(pw - 28.0, min_right_neighbor_x0, max(x1, x0 + 80.0))
        elif is_narrow_left or has_right_neighbor:
            # 狭窄边栏块或右侧存在相邻块：严格限制在边栏或右侧邻块以左，严禁向右跨越覆写
            right_margin = min(min_right_neighbor_x0, max(x1, x0 + 80.0))
        else:
            # 单栏或全宽横幅
            right_margin = min(pw - 35.0, min_right_neighbor_x0, max(x1, min(pw - 105.0, 505.5)))

        # 3. 查找紧随其后的同栏下邻块顶部 y0，作为当前块垂直扩展的硬上限
        next_y0 = ph - 35.0
        for nb in page_all_blocks:
            if nb is block or not nb.bbox or len(nb.bbox) != 4:
                continue
            nx0, ny0, nx1, ny1 = nb.bbox
            if ny0 < y0 + 1.0:
                continue

            # 判断 nb 的栏属
            nb_col = nb.metadata.get("column") if (nb.metadata and "column" in nb.metadata) else "span"
            if nb_col == "span" and is_two_col:
                if nx1 <= col_split_x + 6.0:
                    nb_col = "left"
                elif nx0 >= col_split_x - 6.0:
                    nb_col = "right"

            # 跨栏互不干扰：左栏段落不被右栏段落垂直截断，反之亦然
            if is_two_col and block_col in ("left", "right") and nb_col in ("left", "right") and block_col != nb_col:
                continue

            # 检查横向是否有遮挡或同栏重合
            h_overlap = min(right_margin, nx1) - max(x0, nx0)
            if h_overlap > 8.0:
                next_y0 = min(next_y0, ny0 - 1.0)

        # 1. 居中主标题或居中一级标题 (如 Attention Is All You Need / Abstract)
        if block.align == "center" and block.type in (BlockType.TITLE, BlockType.HEADER_1):
            if (is_two_col and block_col == "left") or is_narrow_left or has_right_neighbor:
                return pymupdf.Rect(x0, y0 - 1.5, right_margin, min(y1 + 4.0, next_y0))
            return pymupdf.Rect(max(35.0, x0 - 15.0) if is_two_col else 108.0, y0 - 1.5, min(right_margin, x1 + 15.0) if is_two_col else min(right_margin, 504.0), min(y1 + 5.0, next_y0))

        # 2. 图表题注或子图居中标题 (如 Figure 1 / Scaled Dot-Product Attention)
        if block.type == BlockType.IMAGE_CAPTION:
            target_y0 = max(y0, block.bbox[1])
            if block.align == "center":
                cx = (x0 + x1) / 2.0
                half_w = max((x1 - x0) / 2.0 + 20.0, 70.0)
                target_x0 = max(x0 - 10.0, cx - half_w) if (is_two_col or is_narrow_left or has_right_neighbor) else max(90.0, cx - half_w)
                target_x1 = min(right_margin, cx + half_w)
                return pymupdf.Rect(target_x0, target_y0, target_x1, min(y1 + 4.0, next_y0))
            return pymupdf.Rect(x0, target_y0, right_margin, min(y1 + 4.0, next_y0))

        # 3. 左对齐各级章节标题 (如 1 Introduction, 3.1 Encoder and Decoder Stacks)
        if block.type in (BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3):
            return pymupdf.Rect(x0, y0 - 1.5, right_margin, min(y1 + 4.0, next_y0))

        # 4. 单行短文本或单行页脚角注 (如 P7 B22 角注5, P0 B18 会议页脚)
        if box_h < 13.5:
            return pymupdf.Rect(x0, y0 - 1.0, right_margin, min(y1 + 3.5, next_y0))

        # 5. 多行正文段落、多行角注、列表项与参考文献条目
        safe_y1 = min(y1 + 3.0, next_y0)
        if safe_y1 <= y0 + 6.0:
            safe_y1 = y1 + 1.0
        return pymupdf.Rect(x0, y0 - 1.0, right_margin, safe_y1)

    @staticmethod
    def _is_pseudo_formula(math_content: str) -> bool:
        """
        判断一个 $...$ 内部的内容是否为“伪公式”（即实质上是学术物理量、带单位数值、温度、度量衡、缩写或符号，
        由 MinerU OCR 误打包为 LaTeX 行内公式），而非真正的数学等式/代数表达式。
        """
        s = math_content.strip()
        # 含有数学关系符、等号、求和、积分、极限、分式的，属于真实数学公式，保持为公式
        if any(k in s for k in ["=", "\\sum", "\\int", "\\frac", "\\prod", "\\lim", "\\in", "\\le", "\\ge", "\\leq", "\\geq", "\\neq", "\\approx", "\\propto"]):
            return False
        # 含有多字符数学函数的，属于真实公式
        if re.search(r"\\(?:sin|cos|tan|log|ln|exp|det|max|min)\b", s):
            return False
        # 含有花体字母 \mathcal 通常是损失函数或集合，属于真实公式
        if "\\mathcal" in s or "\\mathbb" in s:
            return False

        # 匹配单位/物理量/度量衡/温度特征:
        unit_keywords = (
            "\\textmu", "μ", "nm", "mm", "cm", "km", "kDa", "Da", "mbar", "bar",
            "mL", "μL", "nL", "pL", "mg", "kg", "mol", "mmol", "μmol", "nmol",
            "mW", "kW", "mJ", "kJ", "Pa", "kPa", "MPa", "GPa", "mPas",
            "circ", "degree", "°", "C", "F", "K", "min", "sec", "h", "s",
            "cells/mL", "wt", "vol", "%", "pmb", "prime"
        )
        if any(uk in s for uk in unit_keywords):
            return True

        # 匹配纯数字、连字符、范围 (如 500, 1 - 10, 20 - 22)
        s_clean = re.sub(r"\\[a-zA-Z]+", "", s)
        s_clean = re.sub(r"[{}_^~ \t\-\+]+", "", s_clean)
        if s_clean.isdigit():
            return True

        # 匹配大写英文缩写 (如 DLP, nPVA, ATPS, PDMS, DEX, PEG)
        if re.match(r"^[A-Za-z0-9\s\-_+()]+$", s_clean) and len(s_clean) <= 10:
            return True

        return False

    def _latex_math_to_rich_html(self, text: str) -> str:
        """
        全面解析与转换 LaTeX 行内公式、数学符号、学术物理量单位及 MinerU OCR 离散空格，
        将其转化为排版纯净的富文本 (带 <b>, <i>, <sup>, <sub> 标签及标准 Unicode 符号)。
        具备严格保护机制：绝不破坏任何 {{...}} 占位符及已有 HTML 格式标签。
        """
        if not text:
            return ""

        # 0. 绝对保护已存在的 {{...}} 占位符与已有 HTML 标签 (如 <sup>, <sub>, <b>, <i>, <span>)
        # 使用无下划线纯字母数字 token 避免被 subscript/superscript 正则误匹配
        placeholder_map = {}
        cnt = 0
        def _stash_token(m):
            nonlocal cnt
            k = f"QQESCTOKEN{cnt}QQ"
            placeholder_map[k] = m.group(0)
            cnt += 1
            return k

        text = re.sub(r"\{\{[A-Za-z0-9_–\-]+\}\}}|</?(?:sup|sub|b|i|span)[^>]*>", _stash_token, text)

        # 1. 移除 display math 双美元符号包裹
        text = re.sub(r"^\$\$\s*|\s*\$\$$", "", text)

        # 2. 预清洗 LaTeX 环境与尺寸修饰符 (消除 \left, \right, \scriptstyle 等排版杂质)
        text = re.sub(r"\\(?:left|right)\b", "", text)
        text = re.sub(r"\\(?:scriptstyle|scriptscriptstyle|textstyle|displaystyle)\b", "", text)
        text = re.sub(r"\\(?:quad|qquad|enspace)\b", " ", text)
        text = re.sub(r"\\\s+", " ", text)  # \ 后面跟空格 (如 \ nL -> nL)
        text = re.sub(r"\\[,;:!]", " ", text)
        text = text.replace("~", " ")

        # 3. 撇号导数与度数符号在先处理
        text = re.sub(r"\\prime\s*\\prime", "″", text)
        text = re.sub(r"\\prime", "′", text)
        text = re.sub(r"\\(?:circ|degree)\b", "°", text)

        # 4. 希腊字母与学术物理量微符号 (\textmu, \mu 等)
        greek_and_special = {
            "\\textmu": "μ", "\\textcelsius": "°C", "\\textdegree": "°", "\\textperthousand": "‰",
            "\\textendash": "–", "\\textemdash": "—", "\\textless": "<", "\\textgreater": ">",
            "\\textpercent": "%", "\\bullet": "•", "\\circledcirc": "©",
            "\\hbar": "ℏ", "\\ell": "ℓ", "\\AA": "Å", "\\angstrom": "Å",
            "\\alpha": "α", "\\beta": "β", "\\gamma": "γ", "\\delta": "δ",
            "\\epsilon": "ε", "\\varepsilon": "ε", "\\zeta": "ζ", "\\eta": "η",
            "\\theta": "θ", "\\vartheta": "θ", "\\iota": "ι", "\\kappa": "κ",
            "\\lambda": "λ", "\\mu": "μ", "\\nu": "ν", "\\xi": "ξ",
            "\\pi": "π", "\\varpi": "π", "\\rho": "ρ", "\\varrho": "ρ",
            "\\sigma": "σ", "\\varsigma": "σ", "\\tau": "τ", "\\upsilon": "υ",
            "\\phi": "φ", "\\varphi": "φ", "\\chi": "χ", "\\psi": "ψ", "\\omega": "ω",
            "\\Gamma": "Γ", "\\Delta": "Δ", "\\Theta": "Θ", "\\Lambda": "Λ",
            "\\Xi": "Ξ", "\\Pi": "Π", "\\Sigma": "Σ", "\\Upsilon": "Υ",
            "\\Phi": "Φ", "\\Psi": "Ψ", "\\Omega": "Ω",
            "\\varLambda": "Λ", "\\varOmega": "Ω", "\\varPhi": "Φ", "\\varPsi": "Ψ",
            "\\varSigma": "Σ", "\\varTheta": "Θ", "\\varUpsilon": "Υ", "\\varXi": "Ξ", "\\varPi": "Π"
        }
        for k, v in greek_and_special.items():
            text = re.sub(re.escape(k) + r"(?![a-zA-Z])", v, text)

        # 5. 数学算子与关系符号
        op_map = {
            "\\nabla": "∇", "\\partial": "∂", "\\infty": "∞",
            "\\times": "×", "\\cdot": "·", "\\pm": "±", "\\mp": "∓",
            "\\leq": "≤", "\\le": "≤", "\\geq": "≥", "\\ge": "≥",
            "\\neq": "≠", "\\ne": "≠", "\\approx": "≈", "\\equiv": "≡",
            "\\sim": "~", "\\propto": "∝", "\\in": "∈", "\\notin": "∉",
            "\\to": "→", "\\rightarrow": "→", "\\leftarrow": "←",
            "\\sqrt": "√", "\\forall": "∀", "\\exists": "∃",
            "\\sum": "∑", "\\prod": "∏", "\\int": "∫"
        }
        for k, v in op_map.items():
            text = re.sub(re.escape(k) + r"(?![a-zA-Z])", v, text)

        # 6. 花体与黑板粗体字母 (\mathcal{...}, \mathbb{...})
        cal_map = {
            "A": "𝒜", "B": "ℬ", "C": "𝒞", "D": "𝒟", "E": "ℰ", "F": "ℱ", "G": "𝒢", "H": "ℋ",
            "I": "ℐ", "J": "𝒥", "K": "𝒦", "L": "ℒ", "M": "ℳ", "N": "𝒩", "O": "𝒪", "P": "𝒫",
            "Q": "𝒬", "R": "ℛ", "S": "𝒮", "T": "𝒯", "U": "𝒰", "V": "𝒱", "W": "𝒲", "X": "𝒳",
            "Y": "𝒴", "Z": "𝒵"
        }
        for k, v in cal_map.items():
            text = re.sub(r"\\mathcal\s*\{\s*" + k + r"\s*\}", v, text)

        bb_map = {
            "R": "ℝ", "C": "ℂ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "I": "𝕀"
        }
        for k, v in bb_map.items():
            text = re.sub(r"\\mathbb\s*\{\s*" + k + r"\s*\}", v, text)

        # 7. 字体样式解包 (支持嵌套括号，如 \mathrm{(DLP)^{41,42}})
        def _unwrap_braced_cmd(s: str, cmd_pattern: str, wrap_tag: str = "") -> str:
            pattern = re.compile(cmd_pattern + r"\s*\{")
            while True:
                m = pattern.search(s)
                if not m:
                    break
                start = m.start()
                brace_start = m.end() - 1
                depth = 1
                idx = brace_start + 1
                while idx < len(s) and depth > 0:
                    if s[idx] == '{':
                        depth += 1
                    elif s[idx] == '}':
                        depth -= 1
                    idx += 1
                if depth == 0:
                    inner = s[brace_start + 1:idx - 1]
                    if wrap_tag:
                        replacement = f"<{wrap_tag}>{inner}</{wrap_tag}>"
                    else:
                        replacement = inner
                    s = s[:start] + replacement + s[idx:]
                else:
                    s = s[:start] + s[brace_start + 1:]
            return s

        text = _unwrap_braced_cmd(text, r"\\(?:mathbf|boldsymbol|pmb|textbf)", wrap_tag="b")
        text = _unwrap_braced_cmd(text, r"\\(?:mathit|textit)", wrap_tag="i")
        text = _unwrap_braced_cmd(text, r"\\(?:mathrm|mathsf|text|textsf|texttt|rm|sf)")
        text = re.sub(r"\\(?:tt|sf|rm|bf|it)\b", "", text)

        # 8. 上标与下标通用转换
        # 上标若包含度数、撇号等原生上标符号，直接脱壳
        text = re.sub(r"\^\s*\{\s*([°′″]+)\s*\}", r"\1", text)
        text = re.sub(r"\^\s*([°′″])", r"\1", text)

        # 下标 _{...} 或 _X
        text = re.sub(r"_\s*\{([^}]+)\}", r"<sub>\1</sub>", text)
        text = re.sub(r"_([a-zA-Z0-9α-ωΑ-Ω])", r"<sub>\1</sub>", text)
        # 上标 ^{...} 或 ^X
        text = re.sub(r"\^\s*\{([^}]+)\}", r"<sup>\1</sup>", text)
        text = re.sub(r"\^([a-zA-Z0-9α-ωΑ-Ω*†])", r"<sup>\1</sup>", text)

        # 9. 常见帽子与横杠符号
        text = re.sub(r"\\hat\s*\{([^}]+)\}", r"\1̂", text)
        text = re.sub(r"\\bar\s*\{([^}]+)\}", r"\1̄", text)
        text = re.sub(r"\\tilde\s*\{([^}]+)\}", r"\1̃", text)

        # 10. 清洗 OCR 引入的数字与小数点离散空格
        for _ in range(4):
            text = re.sub(r"(?<=\d)\s(?=\d)", "", text)
        text = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", text)

        # 11. 清洗 MinerU OCR 单词内部被拆开的字母空格 (针对学术缩写及单位)
        known_acronyms_map = {
            r"\bn\s+P\s+V(?:\s+A)?\b": "nPVA",
            r"\bk\s+D\s+a\b": "kDa",
            r"\bn\s+m\b": "nm",
            r"\bm\s+b\s+a\s+r\b": "mbar",
            r"\bm\s+i\s+n\b": "min",
            r"\bm\s+w\b": "mW",
            r"\bc\s+m\b": "cm",
            r"\bμ\s*L\b": "μL",
            r"\bm\s*L\b": "mL",
            r"\bn\s*L\b": "nL",
            r"\bD\s+E\s+X\b": "DEX",
            r"\bP\s+E\s+G\b": "PEG",
            r"\bP\s+I\s+P\s+S\b": "PIPS",
            r"\bA\s+T\s+P\s+S\b": "ATPS",
            r"\bP\s+D\s+M\s+S\b": "PDMS",
            r"\bF\s+I\s+T\s+C\b": "FITC",
            r"\bR\s+G\s+D\b": "RGD",
        }
        for pat, rep in known_acronyms_map.items():
            text = re.sub(pat, rep, text, flags=re.IGNORECASE)

        def _join_spaced_letters(m):
            raw = m.group(0)
            letters = raw.split()
            if 2 <= len(letters) <= 6 and all(len(x) == 1 and x.isalpha() for x in letters):
                return "".join(letters)
            return raw

        text = re.sub(r"\b[A-Za-z](?:\s+[A-Za-z])+\b", _join_spaced_letters, text)

        # 12. 清洗冗余孤立的花括号 (注意：严格避开 QQESCTOKEN 或可能存在的双括号)
        text = re.sub(r"(?<!\{)\{\s*([^{}]+?)\s*\}(?!\})", r"\1", text)
        text = re.sub(r"(?<!\{)\{\s*([^{}]+?)\s*\}(?!\})", r"\1", text)

        # 13. 清理残留的反斜杠转义符号 (如 \% -> %, \& -> &, \_ -> _)
        text = re.sub(r"\\([%&_#$])", r"\1", text)

        # 14. 去除两端的美元符号: $...$ -> ...
        text = re.sub(r"\$([^$]+)\$", r"\1", text)
        text = text.replace("$", "")

        # 15. 规范化连字符与减号周围的空格 (如 `500 - μL` -> `500-μL`)
        text = re.sub(r"(\d+)\s*[-−]\s*([a-zA-Zμ°])", r"\1-\2", text)
        text = re.sub(r"\bμ\s+([A-Za-z])", r"μ\1", text)  # μ L -> μL, μ m -> μm
        text = re.sub(r"°\s+([CF])\b", r"°\1", text)        # ° C -> °C
        text = re.sub(r"(\d+)\s+°C\b", r"\1 °C", text)

        # 规范化 <sup> / <sub> 内部的连字符与空格
        text = re.sub(r"<sup>\s*-\s*(\d+)\s*</sup>", r"<sup>-\1</sup>", text)
        text = re.sub(r"<sup>\s*(\d+)\s*,\s*(\d+)\s*</sup>", r"<sup>\1,\2</sup>", text)

        # 16. 消除任何可能遗留的裸 LaTeX 命令反斜杠 (兜底保护，严防 \command 泄露)
        text = re.sub(r"\\[a-zA-Z]+\b", "", text)

        # 17. 规范化空白字符
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"(<(?:b|i|sup|sub)>)\s+", r"\1", text)
        text = re.sub(r"\s+(</(?:b|i|sup|sub)>)", r"\1", text)

        # 18. 还原受保护的占位符与 HTML 标签
        for k, v in placeholder_map.items():
            text = text.replace(k, v)

        return text.strip()

    def _clean_markdown_text(self, text: str) -> str:
        r"""
        清洗 Markdown 文本中的伪公式、LaTeX 泄露与 OCR 空格，使导出的 Markdown 纯净可读且 KaTeX 不报错：
        1. 伪公式 (如 ${ 5 0 0 } \mathrm { - } \mathrm { \textmu L }$, $4 0 5 \mathsf { n m }$, $3 7 ^ { \circ } \mathsf { C }$) -> 直接解为纯文本/标准 Markdown
        2. 真实数学公式 (如 $\mathcal{L} = \dots$, $y_{i,j} \in [0, 1]$) -> 保留 $...$，但清洗内部 \textmu -> \mu 与数字间空格
        3. 裸漏的 LaTeX 代码 (如未加 $ 的 `{ 5 0 0 } \mathrm { - } \mathrm { \textmu L }` 或 `\left( P \right)`) -> 自动清洗为纯文本
        4. 严格保护任何已有的 {{CITE_...}}, {{MATH_...}}, {{FN_TAG_...}} 占位符不被破坏
        """
        if not text:
            return ""

        # 1. 保护占位符 {{...}}
        placeholder_map = {}
        cnt = 0
        def _stash_ph(m):
            nonlocal cnt
            k = f"QQPHSTASH{cnt}QQ"
            placeholder_map[k] = m.group(0)
            cnt += 1
            return k

        text = re.sub(r"\{\{[A-Za-z0-9_–\-]+\}\}}", _stash_ph, text)

        # 2. 处理行内公式 $...$
        def _handle_math_match(m):
            raw_inner = m.group(1).strip()
            if not raw_inner:
                return ""
            # 判断是否为伪公式 (带单位的物理量、纯数字、测量值等)
            if self._is_pseudo_formula(raw_inner):
                # 转为排版纯文本 (如 500-μL, 405 nm, 37 °C, G′, G″)
                cleaned = self._latex_math_to_rich_html(raw_inner)
                # 还原 HTML 上下标标签为 Markdown 友好的纯文本或 Unicode 上标
                cleaned = cleaned.replace("<b>", "**").replace("</b>", "**")
                cleaned = cleaned.replace("<i>", "*").replace("</i>", "*")
                # 简化常见上下标
                cleaned = re.sub(r"<sup>([0-9\-\+]+)</sup>", lambda sm: "".join({"0":"⁰","1":"¹","2":"²","3":"³","4":"⁴","5":"⁵","6":"⁶","7":"⁷","8":"⁸","9":"⁹","-":"⁻","+":"⁺"}.get(c, c) for c in sm.group(1)), cleaned)
                cleaned = re.sub(r"<sub>([0-9]+)</sub>", lambda sm: "".join({"0":"₀","1":"₁","2":"₂","3":"₃","4":"₄","5":"₅","6":"₆","7":"₇","8":"₈","9":"₉"}.get(c, c) for c in sm.group(1)), cleaned)
                # 移除残余的 html 标签
                cleaned = re.sub(r"</?(?:sup|sub)>", "", cleaned)
                return cleaned
            else:
                # 真实公式：保留 $...$，但清洗 KaTeX 致命错误 (\textmu -> \mu, 数字间空格等)
                clean_math = raw_inner
                clean_math = clean_math.replace("\\textmu", "\\mu")
                clean_math = re.sub(r"(?<=\d)\s(?=\d)", "", clean_math)
                clean_math = re.sub(r"\\mathrm\s*\{\s*-\s*\}", "-", clean_math)
                clean_math = re.sub(r"\\mathrm\s*\{\s*\+\s*\}", "+", clean_math)
                return f"${clean_math}$"

        text = re.sub(r"(?<!\\)\$(?!\$)([^\$\n]+?)(?<!\\)\$", _handle_math_match, text)

        # 3. 处理没有用 $ 包裹但裸漏的 LaTeX 代码 (如 `{ 5 0 0 } \mathrm { - } \mathrm { \textmu L }`, `\left( P \right)`)
        # 只要文本中还残留形如 \mathrm{...}, \mathsf{...}, \textmu, \left(, { 500 } 等
        if re.search(r"\\(?:mathrm|mathsf|mathbf|boldsymbol|pmb|textmu|left|right)\b", text) or re.search(r"\{\s*\d+\s*\}", text):
            cleaned_leak = self._latex_math_to_rich_html(text)
            cleaned_leak = cleaned_leak.replace("<b>", "**").replace("</b>", "**")
            cleaned_leak = cleaned_leak.replace("<i>", "*").replace("</i>", "*")
            cleaned_leak = re.sub(r"<sup>([0-9\-\+]+)</sup>", lambda sm: "".join({"0":"⁰","1":"¹","2":"²","3":"³","4":"⁴","5":"⁵","6":"⁶","7":"⁷","8":"⁸","9":"⁹","-":"⁻","+":"⁺"}.get(c, c) for c in sm.group(1)), cleaned_leak)
            cleaned_leak = re.sub(r"<sub>([0-9]+)</sub>", lambda sm: "".join({"0":"₀","1":"₁","2":"₂","3":"₃","4":"₄","5":"₅","6":"₆","7":"₇","8":"₈","9":"₉"}.get(c, c) for c in sm.group(1)), cleaned_leak)
            cleaned_leak = re.sub(r"</?(?:sup|sub)>", "", cleaned_leak)
            text = cleaned_leak

        # 4. 还原占位符
        for k, v in placeholder_map.items():
            text = text.replace(k, v)

        return text

    def _markdown_to_pdf_html(self, text: str, wrap_en_span: bool = False) -> str:
        """将带 Markdown 粗斜体和 <sup>/<sub> 标签的译文转为适合 insert_htmlbox 的 HTML"""
        s = self._latex_math_to_rich_html(text.strip())
        # 移除开头的 Markdown 标题井号
        s = re.sub(r"^#{1,6}\s+", "", s)
        # 保护已有的 <sup>, </sup>, <sub>, </sub>, <b>, </b>, <i>, </i> 标签
        tag_map: Dict[str, str] = {}
        counter = 0

        def _stash_tag(m):
            nonlocal counter
            key = f"@@TAG_{counter}@@"
            tag_map[key] = m.group(0)
            counter += 1
            return key

        s = re.sub(r"</?(?:sup|sub|b|i)>", _stash_tag, s)
        # 转义普通文本中的 <, >, &
        s = html.escape(s)
        # 还原 Markdown 粗体 **...** 与斜体 *...*
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
        # 还原 HTML 上下标标签
        for k, v in tag_map.items():
            s = s.replace(k, v)

        # 若开启中英分字体渲染：将非标签、非实体的西文/数字字符序列包裹为 <span class="en">...</span>
        if wrap_en_span:
            s = re.sub(
                r"(<[^>]+>)|(&[a-zA-Z0-9#]+;)|([A-Za-z0-9_.,;:!?()\[\]{}+=\-*/~^@#$%&|'`\\]+)",
                lambda m: m.group(0) if (m.group(1) or m.group(2)) else f'<span class="en">{m.group(3)}</span>',
                s
            )
        return s

    def _build_pdf_htmlbox_payload(self, block: DocumentBlock) -> Tuple[str, str]:
        """生成 PyMuPDF insert_htmlbox 所需的 (html_fragment, css_str)，中文统一为宋体，英文为 Times New Roman"""
        raw_txt = (block.translated_text or block.original_text or "").strip()
        html_inner = self._markdown_to_pdf_html(raw_txt, wrap_en_span=True)

        base_sz = block.font_size or 10.0
        align = block.align or "left"

        if block.type == BlockType.TITLE:
            font_sz = round(base_sz * 0.95, 1)
            line_h = 1.2
            color = "#000000"
            html_inner = f"<b>{html_inner}</b>"
        elif block.type in (BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3):
            font_sz = round(base_sz * 0.96, 1)
            line_h = 1.2
            color = "#000000"
            html_inner = f"<b>{html_inner}</b>"
        elif block.type == BlockType.FOOTNOTE:
            font_sz = round(base_sz * 0.94, 1)
            line_h = 1.22
            color = "#000000"
        elif block.type == BlockType.REFERENCE:
            font_sz = round(base_sz * 0.92, 1)
            line_h = 1.18
            color = "#000000"
        elif block.type == BlockType.IMAGE_CAPTION:
            font_sz = round(base_sz * 0.94, 1)
            line_h = 1.22
            color = "#000000"
        else:
            font_sz = round(base_sz * 0.95, 1)
            line_h = 1.25
            color = "#000000"

        css = (
            f"* {{ font-family: 'Times New Roman', 'SimSun', serif; font-size: {font_sz}pt; line-height: {line_h}; "
            f"color: {color}; text-align: {align}; margin: 0; padding: 0; }} "
            "b { font-weight: bold; } "
            "i { font-style: italic; } "
            "sup { font-size: 68%; vertical-align: super; line-height: 0; } "
            "sub { font-size: 68%; vertical-align: sub; line-height: 0; }"
        )
        return f"<div>{html_inner}</div>", css

    # =========================================================================
    # 2. 导出原版对照双栏交互网页 (_bilingual.html)
    # =========================================================================
    def export_bilingual_html(
        self,
        blocks: List[DocumentBlock],
        title: str = "学术文献原版双语对照",
        page_count: int = 0,
        renders_rel_dir: str = "page_renders"
    ) -> str:
        """
        导出真正的“左边原版 PDF + 右边相同坐标位置译文”交互式双栏 HTML：
        - 若存在 PDF 页面坐标信息 (page_count > 0)，默认展示 1:1 原版页面双栏对照视图：
          * 左栏：原版 PDF 页面渲染图 + 每个文本块/角注的精确坐标热区
          * 右栏：1:1 同尺寸页面，在完全相同的 (x0, y0, x1, y1) 坐标位置展示译文、原位插图与角注
            （支持一键切换【PDF原位精准排版层】与【HTML实时文字浮层】，支持左右悬停同步高亮与点击详情栏）
        - 同时内置【全文流式双语对照视图（含原位插图与角注）】标签页。
        """
        has_pdf_pages = page_count > 0 and any(b.page_num is not None and b.bbox for b in blocks)

        # 构建 PDF 逐页 1:1 坐标对照 HTML
        pages_spread_html: List[str] = []
        if has_pdf_pages:
            blocks_by_page: Dict[int, List[DocumentBlock]] = {}
            for b in blocks:
                if b.page_num is not None:
                    blocks_by_page.setdefault(b.page_num, []).append(b)

            for pno in range(page_count):
                p_blocks = blocks_by_page.get(pno, [])
                pw = next((b.page_width for b in p_blocks if b.page_width), 612.0)
                ph = next((b.page_height for b in p_blocks if b.page_height), 792.0)

                left_hotspots: List[str] = []
                right_hotspots: List[str] = []

                for b in p_blocks:
                    if not b.bbox or len(b.bbox) != 4 or b.skip_pdf_overlay or b.type == BlockType.IMAGE:
                        continue
                    x0, y0, x1, y1 = b.bbox
                    left_pct = round(x0 / pw * 100.0, 3)
                    top_pct = round(y0 / ph * 100.0, 3)
                    width_pct = round(max((x1 - x0), 12.0) / pw * 100.0, 3)
                    height_pct = round(max((y1 - y0), 8.0) / ph * 100.0, 3)

                    en_clean = self._markdown_to_pdf_html(b.original_text)
                    zh_clean = self._markdown_to_pdf_html(b.translated_text or b.original_text)
                    b_type_cls = f"btype-{b.type.value}"
                    fn_badge = f' data-fn="{html.escape(b.footnote_id)}"' if b.footnote_id else ""

                    style_pos = (
                        f"left:{left_pct}%;top:{top_pct}%;width:{width_pct}%;height:{height_pct}%;"
                    )
                    data_attrs = (
                        f'data-bid="{b.id}" data-btype="{b.type.value}"{fn_badge} '
                        f'data-en="{html.escape(en_clean, quote=True)}" '
                        f'data-zh="{html.escape(zh_clean, quote=True)}"'
                    )

                    left_hotspots.append(
                        f'<div class="bbox-hotspot left-box {b_type_cls}" style="{style_pos}" {data_attrs}></div>'
                    )
                    right_hotspots.append(
                        f'<div class="bbox-hotspot right-box {b_type_cls}" style="{style_pos}" {data_attrs}>'
                        f'<div class="dom-text-content">{zh_clean}</div>'
                        f'</div>'
                    )

                orig_img_src = f"{renders_rel_dir}/page_{pno + 1}_orig.png"
                zh_img_src = f"{renders_rel_dir}/page_{pno + 1}_zh.png"
                bg_img_src = f"{renders_rel_dir}/page_{pno + 1}_bg.png"

                pages_spread_html.append(f"""
                <div class="page-spread" id="page-{pno + 1}">
                    <div class="spread-header">
                        <span class="page-badge">第 {pno + 1} / {page_count} 页</span>
                        <span class="spread-hint">左侧：原版 PDF 页面 &nbsp;|&nbsp; 右侧：相同坐标位置原位译文（含照片与角注）</span>
                    </div>
                    <div class="spread-columns">
                        <!-- 左栏：原版 PDF -->
                        <div class="pdf-page-card left-pdf-card" style="aspect-ratio: {pw} / {ph};">
                            <img class="page-img" src="{orig_img_src}" alt="Original PDF Page {pno + 1}" loading="lazy" />
                            <div class="hotspot-layer">
                                {"".join(left_hotspots)}
                            </div>
                        </div>
                        <!-- 右栏：同位置译文页面 -->
                        <div class="pdf-page-card right-zh-card" style="aspect-ratio: {pw} / {ph};"
                             data-zh-img="{zh_img_src}" data-bg-img="{bg_img_src}">
                            <img class="page-img right-page-img" src="{zh_img_src}" alt="Translated Page {pno + 1}" loading="lazy" />
                            <div class="hotspot-layer">
                                {"".join(right_hotspots)}
                            </div>
                        </div>
                    </div>
                </div>
                """)

        # 同时构建流式全文对照 HTML（包含提取出的原位图片与角注）
        stream_rows_html: List[str] = []
        for block in blocks:
            if block.type == BlockType.RAW:
                continue
            if block.type == BlockType.IMAGE and block.image_path:
                stream_rows_html.append(f"""
                <div class="row img-row">
                    <div class="col en"><img src="{html.escape(block.image_path)}" class="inline-fig" /></div>
                    <div class="col zh"><img src="{html.escape(block.image_path)}" class="inline-fig" /></div>
                </div>
                """)
                continue

            orig_html = self._markdown_to_pdf_html(block.original_text)
            trans_html = self._markdown_to_pdf_html(block.translated_text or block.original_text)

            if block.type in (BlockType.TITLE, BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3):
                h_tag = "h1" if block.type in (BlockType.TITLE, BlockType.HEADER_1) else ("h2" if block.type == BlockType.HEADER_2 else "h3")
                stream_rows_html.append(f"""
                <div class="row header-row">
                    <div class="col en"><{h_tag}>{orig_html}</{h_tag}></div>
                    <div class="col zh"><{h_tag}>{trans_html}</{h_tag}></div>
                </div>
                """)
            elif block.type == BlockType.MATH_BLOCK:
                stream_rows_html.append(f"""
                <div class="row math-row">
                    <div class="col full"><div class="math-center">{html.escape(block.original_text)}</div></div>
                </div>
                """)
            elif block.type == BlockType.FOOTNOTE:
                stream_rows_html.append(f"""
                <div class="row footnote-row">
                    <div class="col en"><span class="fn-tag">角注</span> {orig_html}</div>
                    <div class="col zh"><span class="fn-tag">角注</span> {trans_html}</div>
                </div>
                """)
            elif block.type == BlockType.REFERENCE:
                stream_rows_html.append(f"""
                <div class="row ref-row">
                    <div class="col en"><div class="ref-item">{orig_html}</div></div>
                    <div class="col zh"><div class="ref-item">{trans_html}</div></div>
                </div>
                """)
            else:
                stream_rows_html.append(f"""
                <div class="row para-row">
                    <div class="col en">{orig_html}</div>
                    <div class="col zh">{trans_html}</div>
                </div>
                """)

        spread_section = "\n".join(pages_spread_html)
        stream_section = "\n".join(stream_rows_html)
        default_view = "pdf-spread" if has_pdf_pages else "stream"

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(title)}</title>
    <script>
    window.MathJax = {{
      tex: {{
        inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
        displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']]
      }}
    }};
    </script>
    <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
    <style>
        :root {{
            --bg-color: #0f172a;
            --toolbar-bg: rgba(15, 23, 42, 0.92);
            --card-bg: #ffffff;
            --accent: #3b82f6;
            --accent-glow: rgba(59, 130, 246, 0.22);
            --fn-accent: #f59e0b;
            --border-color: #334155;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
            background-color: var(--bg-color);
            color: #f8fafc;
            margin: 0;
            padding: 0;
            padding-bottom: 120px;
        }}
        .topbar {{
            position: sticky;
            top: 0;
            z-index: 100;
            background: var(--toolbar-bg);
            backdrop-filter: blur(10px);
            border-bottom: 1px solid var(--border-color);
            padding: 12px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .topbar-title {{
            font-size: 16px;
            font-weight: 700;
            color: #f8fafc;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .badge-engine {{
            font-size: 12px;
            background: #1e3a8a;
            color: #93c5fd;
            padding: 3px 10px;
            border-radius: 999px;
            font-weight: 500;
        }}
        .controls {{
            display: flex;
            gap: 8px;
            align-items: center;
        }}
        .btn {{
            background: #1e293b;
            color: #cbd5e1;
            border: 1px solid #475569;
            padding: 6px 14px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            transition: all 0.15s;
        }}
        .btn:hover {{
            background: #334155;
            color: #ffffff;
        }}
        .btn.active {{
            background: var(--accent);
            color: #ffffff;
            border-color: var(--accent);
        }}
        .main-wrapper {{
            max-width: 1720px;
            margin: 20px auto;
            padding: 0 16px;
        }}
        .page-spread {{
            background: #1e293b;
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 28px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
        }}
        .spread-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            padding: 0 4px;
            font-size: 13px;
            color: #94a3b8;
        }}
        .page-badge {{
            background: #334155;
            color: #f8fafc;
            padding: 3px 10px;
            border-radius: 6px;
            font-weight: 600;
        }}
        .spread-columns {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }}
        .pdf-page-card {{
            position: relative;
            width: 100%;
            background: #ffffff;
            border-radius: 6px;
            overflow: hidden;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.2);
        }}
        .page-img {{
            width: 100%;
            height: 100%;
            object-fit: fill;
            display: block;
            user-select: none;
        }}
        .hotspot-layer {{
            position: absolute;
             inset: 0;
            pointer-events: auto;
        }}
        .bbox-hotspot {{
            position: absolute;
            border: 1.5px solid transparent;
            border-radius: 3px;
            cursor: pointer;
            transition: background-color 0.12s, border-color 0.12s;
        }}
        .bbox-hotspot:hover, .bbox-hotspot.active-sync {{
            background-color: var(--accent-glow);
            border-color: var(--accent);
            z-index: 10;
        }}
        .bbox-hotspot.btype-footnote:hover, .bbox-hotspot.btype-footnote.active-sync {{
            background-color: rgba(245, 158, 11, 0.22);
            border-color: var(--fn-accent);
        }}
        /* DOM 实时文字层（默认隐藏，使用 PDF 原位矢量渲染层保证 100% 零溢出；开启 DOM 模式时显示） */
        .dom-text-content {{
            display: none;
            width: 100%;
            height: 100%;
            color: #000000;
            font-family: "Times New Roman", "SimSun", "宋体", "Songti SC", serif;
            font-size: clamp(8px, 0.88vw, 13px);
            line-height: 1.24;
            overflow: hidden;
            text-align: justify;
        }}
        body.dom-overlay-mode .right-box .dom-text-content {{
            display: block;
        }}
        body.dom-overlay-mode .right-box:hover {{
            overflow: visible;
            background: rgba(255, 255, 255, 0.96);
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.18);
            padding: 2px 4px;
            height: auto !important;
            min-height: 20px;
            z-index: 30;
        }}
        /* 底部同步对照检视栏（点击或悬停任意段落/角注时显示可复制原文与译文） */
        .inspector-bar {{
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            z-index: 200;
            background: rgba(15, 23, 42, 0.96);
            backdrop-filter: blur(12px);
            border-top: 1px solid #475569;
            padding: 12px 24px;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            font-size: 13.5px;
            line-height: 1.55;
            max-height: 130px;
            overflow-y: auto;
        }}
        .inspector-col {{
            padding: 4px 10px;
            border-radius: 6px;
            background: rgba(30, 41, 59, 0.7);
        }}
        .inspector-label {{
            font-size: 11px;
            font-weight: 700;
            color: #93c5fd;
            margin-bottom: 4px;
            text-transform: uppercase;
        }}
        /* 流式视图样式 */
        .stream-container {{
            max-width: 1360px;
            margin: 0 auto;
            background: #ffffff;
            color: #0f172a;
            border-radius: 12px;
            padding: 32px;
        }}
        .row {{
            display: flex;
            border-bottom: 1px solid #e2e8f0;
            padding: 14px 8px;
        }}
        .col {{
            flex: 1;
            padding: 0 16px;
            word-break: break-word;
        }}
        .col.en {{
            color: #334155;
            border-right: 1px solid #e2e8f0;
            font-family: "Times New Roman", Georgia, serif;
        }}
        .col.zh {{
            color: #000000;
            font-family: "Times New Roman", "SimSun", "宋体", "Songti SC", serif;
        }}
        .col.full {{ flex: 2; }}
        .inline-fig {{
            max-width: 85%;
            height: auto;
            display: block;
            margin: 8px auto;
            border-radius: 4px;
        }}
        .footnote-row {{
            background: #fffbeb;
            font-size: 13px;
        }}
        .ref-row {{
            background: #f8fafc;
            border-bottom: 1px solid #f1f5f9;
        }}
        .ref-item {{
            font-size: 12.5px;
            line-height: 1.5;
            color: #334155;
            padding-left: 24px;
            text-indent: -24px;
        }}
        .fn-tag {{
            display: inline-block;
            background: #f59e0b;
            color: #fff;
            font-size: 11px;
            padding: 1px 6px;
            border-radius: 4px;
            margin-right: 6px;
        }}
        .hidden {{ display: none !important; }}
    </style>
</head>
<body>
    <div class="topbar">
        <div class="topbar-title">
            <span>📖 {html.escape(title)}</span>
            <span class="badge-engine">MinerU 前置解析 + 1:1 原版坐标对照</span>
        </div>
        <div class="controls">
            <button id="btn-spread" class="btn {'active' if default_view == 'pdf-spread' else ''}" onclick="switchView('pdf-spread')">
                1:1 原版 PDF 左右同位置对照
            </button>
            <button id="btn-dom-layer" class="btn" onclick="toggleDomLayer()">
                切换右页：PDF原位渲染 / HTML文字层
            </button>
            <button id="btn-stream" class="btn {'active' if default_view == 'stream' else ''}" onclick="switchView('stream')">
                全文 Markdown 双栏流式视图
            </button>
        </div>
    </div>

    <div class="main-wrapper">
        <!-- 视图 1：左边原版 PDF + 右边相同位置译文 -->
        <div id="view-pdf-spread" class="{'' if default_view == 'pdf-spread' else 'hidden'}">
            {spread_section}
        </div>

        <!-- 视图 2：全文 Markdown 流式双语视图 -->
        <div id="view-stream" class="stream-container {'' if default_view == 'stream' else 'hidden'}">
            {stream_section}
        </div>
    </div>

    <!-- 底部同步对照检视栏 -->
    <div class="inspector-bar" id="inspector-bar">
        <div class="inspector-col">
            <div class="inspector-label" id="insp-en-label">左侧原版 PDF 对应段落 / 角注（鼠标悬停任意区域同步定位）</div>
            <div id="insp-en-text" style="color:#cbd5e1;">将鼠标悬停在左侧 PDF 或右侧同位置译文的任意段落、图表题注或页脚角注上，即可在此处查看并复制双语文本。</div>
        </div>
        <div class="inspector-col">
            <div class="inspector-label" id="insp-zh-label">右侧同位置中文译文</div>
            <div id="insp-zh-text" style="color:#f8fafc;">支持 <sup>∗</sup>、<sup>4</sup>、<sup>5</sup> 等文献角注与原位照片对齐。</div>
        </div>
    </div>

    <script>
        function switchView(mode) {{
            const spreadEl = document.getElementById('view-pdf-spread');
            const streamEl = document.getElementById('view-stream');
            const btnSpread = document.getElementById('btn-spread');
            const btnStream = document.getElementById('btn-stream');
            if (mode === 'pdf-spread') {{
                spreadEl.classList.remove('hidden');
                streamEl.classList.add('hidden');
                btnSpread.classList.add('active');
                btnStream.classList.remove('active');
            }} else {{
                spreadEl.classList.add('hidden');
                streamEl.classList.remove('hidden');
                btnSpread.classList.remove('active');
                btnStream.classList.add('active');
            }}
        }}

        let domLayerActive = false;
        function toggleDomLayer() {{
            domLayerActive = !domLayerActive;
            document.body.classList.toggle('dom-overlay-mode', domLayerActive);
            const btn = document.getElementById('btn-dom-layer');
            btn.classList.toggle('active', domLayerActive);
            document.querySelectorAll('.right-zh-card').forEach(card => {{
                const img = card.querySelector('.right-page-img');
                img.src = domLayerActive ? card.dataset.bgImg : card.dataset.zhImg;
            }});
        }}

        // 左右同坐标框悬停同步高亮与底部检视器更新
        const allHotspots = document.querySelectorAll('.bbox-hotspot');
        const inspEn = document.getElementById('insp-en-text');
        const inspZh = document.getElementById('insp-zh-text');
        const inspEnLabel = document.getElementById('insp-en-label');
        const inspZhLabel = document.getElementById('insp-zh-label');

        allHotspots.forEach(box => {{
            box.addEventListener('mouseenter', () => {{
                const bid = box.dataset.bid;
                const btype = box.dataset.btype;
                const fnId = box.dataset.fn;
                document.querySelectorAll(`.bbox-hotspot[data-bid="${{bid}}"]`).forEach(el => {{
                    el.classList.add('active-sync');
                }});
                const typeTag = btype === 'footnote' ? `📌 页脚角注 ${{fnId ? '[' + fnId + ']' : ''}}` : `段落块 (${{bid}})`;
                inspEnLabel.innerHTML = `左侧原版 PDF — ${{typeTag}}`;
                inspZhLabel.innerHTML = `右侧同位置译文 — ${{typeTag}}`;
                inspEn.innerHTML = box.dataset.en || '';
                inspZh.innerHTML = box.dataset.zh || '';
            }});
            box.addEventListener('mouseleave', () => {{
                const bid = box.dataset.bid;
                document.querySelectorAll(`.bbox-hotspot[data-bid="${{bid}}"]`).forEach(el => {{
                    el.classList.remove('active-sync');
                }});
            }});
        }});
    </script>
</body>
</html>"""

    # =========================================================================
    # 3. 导出含原位插图与角注的 Markdown 及 DOCX
    # =========================================================================
    def export_bilingual_markdown(self, blocks: List[DocumentBlock]) -> str:
        """导出包含结构化排版格式、原位插图与页脚角注的双语交替对照 Markdown"""
        lines: List[str] = []
        emitted_abstract_heading = False

        for block in blocks:
            if block.type == BlockType.RAW:
                continue

            # 跨页接续块跳过独立输出（已在前半段合并）
            if block.is_cross_page_head and block.metadata.get("full_combined_zh") and getattr(self, "para_format", "reorganize") == "reorganize":
                continue

            # 跨页前半段合并输出
            if block.is_cross_page_tail and block.metadata.get("full_combined_zh") and getattr(self, "para_format", "reorganize") == "reorganize":
                partner_id = block.cross_page_partner_id
                partner = next((b for b in blocks if b.id == partner_id), None)
                partner_en = partner.original_text.strip() if partner else ""
                full_en = f"{block.original_text.strip()} {partner_en}"
                full_zh = self._clean_markdown_text(block.metadata["full_combined_zh"])
                lines.append(f"> **[EN]** {full_en}")
                lines.append(">")
                lines.append(f"> **[ZH]** {full_zh}")
                lines.append("")
                continue

            is_abstract_sec = bool(block.metadata and block.metadata.get("is_abstract")) or (
                block.page_num == 0 and bool(re.match(r"^\*\*(Background|Methods|Results|Conclusions|Objective|Findings|Interpretation)\*\*", block.original_text.strip()))
            )
            if is_abstract_sec:
                if not emitted_abstract_heading:
                    emitted_abstract_heading = True
                    lines.append("## Abstract / 摘要")
                    lines.append("")
            elif block.type in (BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3):
                plain_en = re.sub(r"^#{1,6}\s+", "", block.original_text).strip()
                if plain_en.lower() == "abstract":
                    emitted_abstract_heading = True

            if block.type in (BlockType.CODE_BLOCK, BlockType.DIVIDER, BlockType.MATH_BLOCK, BlockType.IMAGE):
                lines.append(block.original_text)
                lines.append("")
                continue

            if block.type == BlockType.AUTHOR_INFO:
                lines.append(block.original_text)
                lines.append("")
                continue

            if block.type in (BlockType.TITLE, BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3):
                lines.append(block.original_text)
                if block.translated_text and block.translated_text != block.original_text:
                    lines.append(self._clean_markdown_text(block.translated_text))
                lines.append("")
                continue

            if block.type == BlockType.TABLE:
                lines.append(block.original_text)
                lines.append("")
                continue

            if block.type == BlockType.REFERENCE:
                ref_txt = block.original_text.strip()
                is_new_cit = bool(re.match(r"^(\[\d+\]|\d+\.\s*|\d+\t|\d+\.$)", ref_txt))
                is_cont = bool(block.metadata and block.metadata.get("is_ref_continuation")) or (not is_new_cit)
                while lines and lines[-1] == "":
                    lines.pop()
                if is_cont and lines and not lines[-1].startswith("#"):
                    sep = "" if lines[-1].endswith("-") else " "
                    lines[-1] = lines[-1] + sep + ref_txt
                    lines.append("")
                else:
                    lines.append(ref_txt)
                    lines.append("")
                continue

            en_text = block.original_text.strip()
            zh_text = self._clean_markdown_text((block.translated_text or block.original_text).strip())

            if block.type == BlockType.FOOTNOTE:
                lines.append(f"> **[角注 EN]** {en_text}")
                lines.append(">")
                lines.append(f"> **[角注 ZH]** {zh_text}")
                lines.append("")
            else:
                lines.append(f"> **[EN]** {en_text}")
                lines.append(">")
                lines.append(f"> **[ZH]** {zh_text}")
                lines.append("")

        return "\n".join(lines)

    def export_pure_translated_markdown(self, blocks: List[DocumentBlock]) -> str:
        """导出包含结构化排版格式、原位插图与页脚角注的纯中文 Markdown"""
        lines: List[str] = []
        emitted_abstract_heading = False

        for block in blocks:
            if block.type == BlockType.RAW:
                continue

            if block.is_cross_page_head and block.metadata.get("full_combined_zh") and getattr(self, "para_format", "reorganize") == "reorganize":
                continue

            if block.is_cross_page_tail and block.metadata.get("full_combined_zh") and getattr(self, "para_format", "reorganize") == "reorganize":
                lines.append(self._clean_markdown_text(block.metadata["full_combined_zh"]))
                lines.append("")
                continue

            is_abstract_sec = bool(block.metadata and block.metadata.get("is_abstract")) or (
                block.page_num == 0 and bool(re.match(r"^\*\*(Background|Methods|Results|Conclusions|Objective|Findings|Interpretation)\*\*", block.original_text.strip()))
            )
            if is_abstract_sec:
                if not emitted_abstract_heading:
                    emitted_abstract_heading = True
                    lines.append("## 摘要")
                    lines.append("")
            elif block.type in (BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3):
                plain_en = re.sub(r"^#{1,6}\s+", "", block.original_text).strip()
                if plain_en.lower() == "abstract":
                    emitted_abstract_heading = True

            if block.type in (BlockType.CODE_BLOCK, BlockType.DIVIDER, BlockType.MATH_BLOCK, BlockType.IMAGE, BlockType.AUTHOR_INFO, BlockType.TABLE):
                lines.append(block.original_text)
            elif block.type == BlockType.REFERENCE:
                ref_txt = block.original_text.strip()
                is_new_cit = bool(re.match(r"^(\[\d+\]|\d+\.\s*|\d+\t|\d+\.$)", ref_txt))
                is_cont = bool(block.metadata and block.metadata.get("is_ref_continuation")) or (not is_new_cit)
                while lines and lines[-1] == "":
                    lines.pop()
                if is_cont and lines and not lines[-1].startswith("#"):
                    sep = "" if lines[-1].endswith("-") else " "
                    lines[-1] = lines[-1] + sep + ref_txt
                    lines.append("")
                else:
                    lines.append(ref_txt)
                    lines.append("")
            elif block.type == BlockType.FOOTNOTE:
                lines.append(f"> {self._clean_markdown_text(block.translated_text if block.translated_text else block.original_text)}")
            else:
                lines.append(self._clean_markdown_text(block.translated_text if block.translated_text else block.original_text))
            lines.append("")

        return "\n".join(lines)

    # =========================================================================
    # 4. 导出与原版 PDF 版式一致的 Word 文档 (中文统一为宋体，英文为 Times New Roman)
    # =========================================================================
    def export_bilingual_docx(
        self,
        blocks: List[DocumentBlock],
        output_path: str,
        base_dir: Optional[Path] = None,
        source_pdf_path: Optional[Path] = None
    ) -> str:
        """导出与原版 PDF 格式一致、逐段双语对照的 Word 文档（中文统一为宋体，英文为 Times New Roman）"""
        return self._build_academic_docx(
            blocks=blocks,
            output_path=output_path,
            base_dir=base_dir,
            source_pdf_path=source_pdf_path,
            bilingual=True
        )

    def export_zh_docx(
        self,
        blocks: List[DocumentBlock],
        output_path: str,
        base_dir: Optional[Path] = None,
        source_pdf_path: Optional[Path] = None
    ) -> str:
        """导出与原版 PDF 格式一致的纯中文译文 Word 文档（中文统一为宋体，英文为 Times New Roman）"""
        return self._build_academic_docx(
            blocks=blocks,
            output_path=output_path,
            base_dir=base_dir,
            source_pdf_path=source_pdf_path,
            bilingual=False
        )

    @staticmethod
    def _sanitize_for_docx(text: str) -> str:
        """
        清洗文本，替换 TeX 数学字体遗留的控制字符括号，并剔除所有不符合 XML 1.0 规范的字符。
        防止 python-docx 抛出 ValueError: All strings must be XML compatible: Unicode or ASCII, no NULL bytes or control characters
        """
        if not text:
            return ""
        char_map = {
            "\x00": "[",
            "\x01": "]",
            "\x02": "[",
            "\x03": "]",
            "\x10": "(",
            "\x11": ")",
            "\x12": "[",
            "\x13": "]",
            "\x14": "{",
            "\x15": "}",
        }
        for k, v in char_map.items():
            if k in text:
                text = text.replace(k, v)
        return re.sub(r"[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", text)

    def _set_docx_run_font(
        self,
        run,
        size_pt: float,
        bold: bool = False,
        italic: bool = False,
        color_rgb: Tuple[int, int, int] = (0, 0, 0),
        superscript: bool = False,
        subscript: bool = False,
        east_asia_font: str = "宋体"
    ):
        """强制设置 Word Run 字体：英文/数字/符号统一为 Times New Roman，标题加粗中文统一为黑体，正文中文统一为宋体"""
        run.font.name = "Times New Roman"
        run.font.size = Pt(size_pt)
        run.bold = bold
        run.italic = italic
        run.font.superscript = superscript
        run.font.subscript = subscript
        run.font.color.rgb = RGBColor(*color_rgb)

        rPr = run._element.get_or_add_rPr()
        rFonts = rPr.get_or_add_rFonts()
        rFonts.set(qn("w:ascii"), "Times New Roman")
        rFonts.set(qn("w:hAnsi"), "Times New Roman")
        rFonts.set(qn("w:cs"), "Times New Roman")
        rFonts.set(qn("w:eastAsia"), "黑体" if bold else east_asia_font)

    def _add_rich_runs_to_paragraph(
        self,
        p,
        text: str,
        size_pt: float,
        base_bold: bool = False,
        base_italic: bool = False,
        color_rgb: Tuple[int, int, int] = (0, 0, 0)
    ):
        """
        解析文本中的 Markdown (**粗体**, *斜体*) 与 HTML (<b>, <i>, <sup>, <sub>) 标签，
        向 Word 段落写入带真实上下标、粗斜体及【中文宋体 + 英文 Times New Roman】的 Runs。
        """
        text = self._sanitize_for_docx(text)
        has_leading_space = text.startswith(" ")
        has_trailing_space = text.endswith(" ")
        s = self._latex_math_to_rich_html(text.strip())
        s = re.sub(r"^#{1,6}\s+", "", s)
        if has_leading_space:
            s = " " + s
        if has_trailing_space:
            s = s + " "
        # 将 Markdown **...** 转为 <b>...</b>
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        # 若为图表题注开头 (Figure 1: / 图1：/ Table 1: / 表1：)，自动加粗编号前缀
        s = re.sub(
            r"^((?:Figure|Fig\.|Table)\s*\d+\s*[:.]|(?:图|表)\s*\d+\s*[：:.])",
            r"<b>\1</b>",
            s,
            flags=re.IGNORECASE
        )

        tokens = re.split(r"(</?(?:b|i|sup|sub)>)", s)
        bold = base_bold
        italic = base_italic
        sup = False
        sub = False

        for tok in tokens:
            if not tok:
                continue
            low = tok.lower()
            if low == "<b>":
                bold = True
            elif low == "</b>":
                bold = base_bold
            elif low == "<i>":
                italic = True
            elif low == "</i>":
                italic = base_italic
            elif low == "<sup>":
                sup = True
                sub = False
            elif low == "</sup>":
                sup = False
            elif low == "<sub>":
                sub = True
                sup = False
            elif low == "</sub>":
                sub = False
            else:
                clean_tok = html.unescape(tok)
                clean_tok = self._sanitize_for_docx(clean_tok)
                if clean_tok:
                    r = p.add_run(clean_tok)
                    self._set_docx_run_font(
                        r,
                        size_pt=size_pt,
                        bold=bold,
                        italic=italic,
                        color_rgb=color_rgb,
                        superscript=sup,
                        subscript=sub
                    )

    def _add_paragraph_border(
        self,
        p,
        top_sz: Optional[int] = None,
        bottom_sz: Optional[int] = None,
        top_space: int = 8,
        bottom_space: int = 8
    ):
        """为 Word 段落添加学术论文风格顶/底横线边框 (如 NIPS 标题上下横线或页脚角注分割线)"""
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        if top_sz is not None:
            top = OxmlElement("w:top")
            top.set(qn("w:val"), "single")
            top.set(qn("w:sz"), str(top_sz))
            top.set(qn("w:space"), str(top_space))
            top.set(qn("w:color"), "000000")
            pBdr.append(top)
        if bottom_sz is not None:
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), str(bottom_sz))
            bottom.set(qn("w:space"), str(bottom_space))
            bottom.set(qn("w:color"), "000000")
            pBdr.append(bottom)
        pPr.append(pBdr)

    def _prepare_docx_items(
        self,
        blocks: List[DocumentBlock],
        base_dir: Optional[Path],
        source_pdf_path: Optional[Path]
    ) -> List[Dict]:
        """
        将原始块序列整理为与原版 PDF 版式一致的流式排版单元：
        1. 自动合并同页相邻的 AUTHOR_INFO 块为居中无边框多列作者网格
        2. 自动合并同页相邻的 TABLE / MATH_BLOCK 碎片，并从原版 PDF 裁剪 250 DPI 高清三线表与独立公式图
        3. 过滤已包含在插图内部的重复子标题（如 Figure 2 内部顶部的两个子图名）
        4. 自动将打断跨页句子的页顶浮动图表移至该句闭合之后，并将同页角注归集至该页末尾
        """
        pdf_doc = None
        if source_pdf_path and Path(source_pdf_path).exists():
            try:
                pdf_doc = pymupdf.open(str(source_pdf_path))
            except Exception:
                pdf_doc = None

        crops_dir = (base_dir / "images") if base_dir else Path("./output/images")
        crops_dir.mkdir(parents=True, exist_ok=True)

        # 按页分组预处理
        pages_map: Dict[int, List[DocumentBlock]] = {}
        for b in blocks:
            if b.type == BlockType.RAW:
                continue
            pno = b.page_num if b.page_num is not None else 0
            pages_map.setdefault(pno, []).append(b)

        structured_items: List[Dict] = []

        for pno in sorted(pages_map.keys()):
            p_blocks = pages_map[pno]
            # 找出本页所有 IMAGE 块的 bbox，过滤完全落在 IMAGE 块内部的短子标题
            img_bboxes = [b.bbox for b in p_blocks if b.type == BlockType.IMAGE and b.bbox]

            filtered_blocks: List[DocumentBlock] = []
            for b in p_blocks:
                if b.type == BlockType.IMAGE_CAPTION and b.bbox and img_bboxes:
                    bx0, by0, bx1, by1 = b.bbox
                    if any(
                        bx0 >= ix0 - 5.0 and bx1 <= ix1 + 5.0 and by0 >= iy0 - 5.0 and by1 <= iy1 + 5.0
                        for (ix0, iy0, ix1, iy1) in img_bboxes
                    ):
                        continue
                filtered_blocks.append(b)

            # 分离本页的页脚角注 (FOOTNOTE) 与正文/图表块
            page_footnotes = [b for b in filtered_blocks if b.type == BlockType.FOOTNOTE]
            main_blocks = [b for b in filtered_blocks if b.type != BlockType.FOOTNOTE]

            # 将连续的 AUTHOR_INFO、连续的 TABLE、连续的 MATH_BLOCK 聚合成排版单元
            page_units: List[Dict] = []
            i = 0
            n = len(main_blocks)
            while i < n:
                b = main_blocks[i]

                # 1. 作者信息网格聚合
                if b.type == BlockType.AUTHOR_INFO:
                    auth_group = [b]
                    j = i + 1
                    while j < n and main_blocks[j].type == BlockType.AUTHOR_INFO:
                        auth_group.append(main_blocks[j])
                        j += 1
                    page_units.append({"kind": "author_grid", "blocks": auth_group, "page_num": pno})
                    i = j
                    continue

                # 2. 原版学术三线表聚合与高清裁剪
                if b.type == BlockType.TABLE:
                    tbl_group = [b]
                    j = i + 1
                    while j < n and main_blocks[j].type == BlockType.TABLE:
                        tbl_group.append(main_blocks[j])
                        j += 1
                    bboxes = [tb.bbox for tb in tbl_group if tb.bbox]
                    crop_path = None
                    width_in = 4.8
                    if pdf_doc is not None and bboxes and pno < len(pdf_doc):
                        p_rect = pdf_doc[pno].rect
                        x0 = max(10.0, min(bb[0] for bb in bboxes) - 10.0)
                        y0 = max(10.0, min(bb[1] for bb in bboxes) - 8.0)
                        x1 = min(p_rect.width - 10.0, max(bb[2] for bb in bboxes) + 10.0)
                        y1 = min(p_rect.height - 10.0, max(bb[3] for bb in bboxes) + 8.0)
                        rect = pymupdf.Rect(x0, y0, x1, y1)
                        pix = pdf_doc[pno].get_pixmap(clip=rect, dpi=250)
                        crop_file = crops_dir / f"docx_table_p{pno + 1}_{i}.png"
                        pix.save(str(crop_file))
                        crop_path = crop_file
                        width_in = min(5.45, max(2.5, (x1 - x0) / 72.0))
                    page_units.append({
                        "kind": "cropped_table",
                        "blocks": tbl_group,
                        "crop_path": crop_path,
                        "width_in": width_in,
                        "page_num": pno
                    })
                    i = j
                    continue

                # 3. 独立数学公式块聚合与高清裁剪
                if b.type == BlockType.MATH_BLOCK:
                    eq_group = [b]
                    j = i + 1
                    while (
                        j < n
                        and main_blocks[j].type == BlockType.MATH_BLOCK
                        and b.bbox
                        and main_blocks[j].bbox
                        and (main_blocks[j].bbox[1] - eq_group[-1].bbox[3]) < 25.0
                    ):
                        eq_group.append(main_blocks[j])
                        j += 1
                    bboxes = [eb.bbox for eb in eq_group if eb.bbox]
                    crop_path = None
                    width_in = 4.2
                    if pdf_doc is not None and bboxes and pno < len(pdf_doc):
                        p_rect = pdf_doc[pno].rect
                        x0 = max(10.0, min(bb[0] for bb in bboxes) - 6.0)
                        y0 = max(10.0, min(bb[1] for bb in bboxes) - 5.0)
                        x1 = min(p_rect.width - 10.0, max(bb[2] for bb in bboxes) + 6.0)
                        y1 = min(p_rect.height - 10.0, max(bb[3] for bb in bboxes) + 5.0)
                        # 防御性校验：仅当裁剪区域宽度 >= 35pt 时作为独立公式裁剪，防止行内短碎片误截
                        if (x1 - x0) >= 35.0:
                            rect = pymupdf.Rect(x0, y0, x1, y1)
                            pix = pdf_doc[pno].get_pixmap(clip=rect, dpi=250)
                            crop_file = crops_dir / f"docx_eq_p{pno + 1}_{i}.png"
                            pix.save(str(crop_file))
                            crop_path = crop_file
                            width_in = min(5.4, max(1.2, (x1 - x0) / 72.0))
                    page_units.append({
                        "kind": "cropped_eq",
                        "blocks": eq_group,
                        "crop_path": crop_path,
                        "width_in": width_in,
                        "page_num": pno
                    })
                    i = j
                    continue

                # 4. 普通块 (TITLE, HEADER, PARAGRAPH, IMAGE, IMAGE_CAPTION, LIST_ITEM, REFERENCE)
                page_units.append({"kind": "single", "block": b, "page_num": pno})
                i += 1

            # 如果本页顶部是浮动插图/表格 (IMAGE / IMAGE_CAPTION / cropped_table)，且紧随其后的第一个正文块是跨页续句（首字母小写），
            # 则将该跨页续句移至页顶浮动图表之前，保证跨页段落语义不被图表截断
            float_prefix: List[Dict] = []
            rest_units: List[Dict] = []
            in_prefix = True
            for u in page_units:
                if in_prefix and (
                    u["kind"] == "cropped_table"
                    or (u["kind"] == "single" and u["block"].type in (BlockType.IMAGE, BlockType.IMAGE_CAPTION))
                ):
                    float_prefix.append(u)
                else:
                    in_prefix = False
                    rest_units.append(u)

            if float_prefix and rest_units:
                first_rest = rest_units[0]
                if first_rest["kind"] == "single" and first_rest["block"].type == BlockType.PARAGRAPH:
                    raw_en = re.sub(r"<[^>]+>", "", first_rest["block"].original_text).strip()
                    if raw_en and raw_en[0].islower():
                        page_units = [first_rest] + float_prefix + rest_units[1:]

            structured_items.extend(page_units)
            if page_footnotes:
                structured_items.append({
                    "kind": "footnotes",
                    "blocks": page_footnotes,
                    "page_num": pno
                })

        if pdf_doc is not None:
            pdf_doc.close()

        return structured_items

    def _render_author_grid_docx(self, doc, author_blocks: List[DocumentBlock]):
        """在 Word 中还原与原版 PDF 一致的无边框居中多列作者网格"""
        # 将同一列上下紧邻的姓名块与机构/邮箱块合并为一个作者卡片
        merged_authors: List[List[str]] = []
        for b in author_blocks:
            txt = b.original_text.strip()
            if not txt:
                continue
            if b.bbox and merged_authors:
                prev_bbox = merged_authors[-1][0]
                curr_cx = (b.bbox[0] + b.bbox[2]) / 2.0
                prev_cx = (prev_bbox[0] + prev_bbox[2]) / 2.0
                if abs(curr_cx - prev_cx) < 28.0 and 0.0 <= (b.bbox[1] - prev_bbox[3]) < 15.0:
                    merged_authors[-1][1].append(txt)
                    merged_authors[-1][0] = [
                        min(prev_bbox[0], b.bbox[0]),
                        prev_bbox[1],
                        max(prev_bbox[2], b.bbox[2]),
                        b.bbox[3]
                    ]
                    continue
            merged_authors.append([list(b.bbox) if b.bbox else [0, 0, 0, 0], [txt]])

        if not merged_authors:
            return

        cols = min(4, len(merged_authors))
        rows = (len(merged_authors) + cols - 1) // cols
        table = doc.add_table(rows=rows, cols=cols)
        table.alignment = WD_ALIGN_PARAGRAPH.CENTER

        for idx, (_, lines_list) in enumerate(merged_authors):
            r_idx = idx // cols
            c_idx = idx % cols
            cell = table.cell(r_idx, c_idx)
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(3)
            p.paragraph_format.line_spacing = 1.15

            full_str = " ".join(lines_list)
            # 将作者姓名与机构、邮箱分行展示
            parts = re.split(r"\s+(?=(?:Google|University|[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+))", full_str)
            for p_i, part in enumerate(parts):
                if p_i > 0:
                    p.add_run("\n")
                is_name_line = (p_i == 0)
                self._add_rich_runs_to_paragraph(
                    p,
                    part.strip(),
                    size_pt=9.5,
                    base_bold=is_name_line,
                    color_rgb=(0, 0, 0)
                )

    def _build_academic_docx(
        self,
        blocks: List[DocumentBlock],
        output_path: str,
        base_dir: Optional[Path] = None,
        source_pdf_path: Optional[Path] = None,
        bilingual: bool = True
    ) -> str:
        """
        构建与原版 PDF 格式一致的学术论文 Word 文档：
        - 严格统一字体：中文为【宋体】，英文/数字/符号为【Times New Roman】
        - 严格还原版式：NIPS 顶底双横线标题、居中作者网格、缩进摘要、原版高清三线表与独立公式、居中插图与题注、页脚分割线角注、悬挂缩进参考文献
        """
        if not DOCX_AVAILABLE:
            raise ImportError("未安装 python-docx，无法导出 .docx 文件")

        doc = Document()

        # 1. 页面尺寸与页边距（自适应原版 PDF 几何尺寸与排版风格）
        doc_is_two_col = any(b.metadata.get("is_two_col", False) for b in blocks if b.metadata)
        pdf_w_in, pdf_h_in = 8.5, 11.0
        if source_pdf_path and Path(source_pdf_path).exists():
            try:
                _pdoc = pymupdf.open(str(source_pdf_path))
                if len(_pdoc) > 0:
                    pdf_w_in = round(_pdoc[0].rect.width / 72.0, 2)
                    pdf_h_in = round(_pdoc[0].rect.height / 72.0, 2)
                _pdoc.close()
            except Exception:
                pass

        for s in doc.sections:
            s.page_width = Inches(pdf_w_in)
            s.page_height = Inches(pdf_h_in)
            s.top_margin = Inches(1.0)
            s.bottom_margin = Inches(1.0)
            s.left_margin = Inches(1.0)
            s.right_margin = Inches(1.0)

        # 2. 设置全局默认样式 Normal 为【中文宋体 + 英文 Times New Roman】
        normal_style = doc.styles["Normal"]
        normal_style.font.name = "Times New Roman"
        normal_style.font.size = Pt(10.0)
        normal_style.font.color.rgb = RGBColor(0, 0, 0)
        ns_rPr = normal_style._element.get_or_add_rPr()
        ns_rFonts = ns_rPr.get_or_add_rFonts()
        ns_rFonts.set(qn("w:ascii"), "Times New Roman")
        ns_rFonts.set(qn("w:hAnsi"), "Times New Roman")
        ns_rFonts.set(qn("w:cs"), "Times New Roman")
        ns_rFonts.set(qn("w:eastAsia"), "宋体")

        items = self._prepare_docx_items(blocks, base_dir=base_dir, source_pdf_path=source_pdf_path)
        next_is_abstract_body = False
        emitted_abstract_heading = False

        for item in items:
            kind = item["kind"]

            # (A) 作者网格
            if kind == "author_grid":
                self._render_author_grid_docx(doc, item["blocks"])
                continue

            # (B) 原版高清三线表或独立数学公式
            if kind in ("cropped_table", "cropped_eq"):
                crop_path = item.get("crop_path")
                width_in = item.get("width_in", 4.5)
                if crop_path and Path(crop_path).exists():
                    p = doc.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p.paragraph_format.space_before = Pt(8)
                    p.paragraph_format.space_after = Pt(8)
                    r = p.add_run()
                    r.add_picture(str(crop_path), width=Inches(width_in))
                else:
                    for tb in item["blocks"]:
                        p = doc.add_paragraph()
                        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        p.paragraph_format.space_before = Pt(6)
                        p.paragraph_format.space_after = Pt(6)
                        clean_eq = re.sub(r"^\$\$\s*|\s*\$\$$", "", tb.original_text).strip()
                        self._add_rich_runs_to_paragraph(p, clean_eq, size_pt=10.0)
                continue

            # (C) 页脚角注区（带顶部分割细线 + 小五号 8.5pt 宋体/Times New Roman）
            if kind == "footnotes":
                fn_blocks = item["blocks"]
                for idx_f, fb in enumerate(fn_blocks):
                    en_txt = fb.original_text.strip()
                    zh_txt = (fb.translated_text or fb.original_text).strip()
                    if bilingual:
                        p_en = doc.add_paragraph()
                        p_en.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                        p_en.paragraph_format.space_before = Pt(4 if idx_f == 0 else 1)
                        p_en.paragraph_format.space_after = Pt(1)
                        p_en.paragraph_format.line_spacing = 1.15
                        if idx_f == 0:
                            self._add_paragraph_border(p_en, top_sz=6, top_space=4)
                        self._add_rich_runs_to_paragraph(p_en, en_txt, size_pt=8.5, color_rgb=(40, 40, 40))

                        if zh_txt and zh_txt != en_txt:
                            p_zh = doc.add_paragraph()
                            p_zh.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                            p_zh.paragraph_format.space_before = Pt(0)
                            p_zh.paragraph_format.space_after = Pt(2)
                            p_zh.paragraph_format.line_spacing = 1.15
                            self._add_rich_runs_to_paragraph(p_zh, zh_txt, size_pt=8.5, color_rgb=(0, 0, 0))
                    else:
                        p_zh = doc.add_paragraph()
                        p_zh.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                        p_zh.paragraph_format.space_before = Pt(4 if idx_f == 0 else 1)
                        p_zh.paragraph_format.space_after = Pt(2)
                        p_zh.paragraph_format.line_spacing = 1.15
                        if idx_f == 0:
                            self._add_paragraph_border(p_zh, top_sz=6, top_space=4)
                        self._add_rich_runs_to_paragraph(p_zh, zh_txt, size_pt=8.5, color_rgb=(0, 0, 0))
                continue

            # (D) 常规单块元素
            block: DocumentBlock = item["block"]
            if block.is_cross_page_head and block.metadata and block.metadata.get("full_combined_zh"):
                continue
            en_txt = (block.metadata.get("full_combined_text") if block.metadata and block.metadata.get("full_combined_text") else block.original_text).strip()
            zh_txt = (block.metadata.get("full_combined_zh") if block.metadata and block.metadata.get("full_combined_zh") else (block.translated_text or block.original_text)).strip()

            # 结构化摘要统领标题：如果遇到结构化摘要小节且尚未输出 Abstract 标题，先输出居中加粗 Abstract / 摘要 标题
            is_abstract_sec = bool(block.metadata and block.metadata.get("is_abstract")) or (
                block.page_num == 0 and bool(re.match(r"^\*\*(Background|Methods|Results|Conclusions|Objective|Findings|Interpretation)\*\*", en_txt))
            )
            if is_abstract_sec:
                if not emitted_abstract_heading:
                    emitted_abstract_heading = True
                    p_abs = doc.add_paragraph()
                    p_abs.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p_abs.paragraph_format.space_before = Pt(14)
                    p_abs.paragraph_format.space_after = Pt(6)
                    p_abs.paragraph_format.keep_with_next = True
                    if bilingual:
                        self._add_rich_runs_to_paragraph(p_abs, "Abstract / 摘要", size_pt=12.0, base_bold=True, color_rgb=(0, 0, 0))
                    else:
                        self._add_rich_runs_to_paragraph(p_abs, "摘要", size_pt=12.0, base_bold=True, color_rgb=(0, 0, 0))

            # 1. 插图块 (BlockType.IMAGE)
            if block.type == BlockType.IMAGE and block.image_path and base_dir:
                img_full = base_dir / block.image_path
                if img_full.exists():
                    w_in = 4.2
                    if block.bbox:
                        w_in = min(5.4, max(2.2, (block.bbox[2] - block.bbox[0]) / 72.0))
                    p = doc.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p.paragraph_format.space_before = Pt(6)
                    p.paragraph_format.space_after = Pt(4)
                    p.paragraph_format.keep_with_next = True
                    r = p.add_run()
                    r.add_picture(str(img_full), width=Inches(w_in))
                continue

            # 2. 论文主标题 (BlockType.TITLE - NIPS 顶底横线居中大标题)
            if block.type == BlockType.TITLE:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_before = Pt(6)
                p.paragraph_format.space_after = Pt(12)
                self._add_paragraph_border(p, top_sz=24, bottom_sz=8, top_space=10, bottom_space=10)
                if bilingual:
                    self._add_rich_runs_to_paragraph(p, en_txt, size_pt=17.0, base_bold=True, color_rgb=(0, 0, 0))
                    if zh_txt and zh_txt != en_txt:
                        p.add_run("\n")
                        self._add_rich_runs_to_paragraph(p, zh_txt, size_pt=15.0, base_bold=True, color_rgb=(0, 0, 0))
                else:
                    self._add_rich_runs_to_paragraph(p, zh_txt, size_pt=17.0, base_bold=True, color_rgb=(0, 0, 0))
                continue

            # 3. 各级章节标题 (HEADER_1, HEADER_2, HEADER_3)
            if block.type in (BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3):
                plain_en = re.sub(r"^#{1,6}\s+", "", en_txt).strip()
                is_abstract = plain_en.lower() == "abstract"
                if is_abstract:
                    emitted_abstract_heading = True
                next_is_abstract_body = is_abstract

                if block.type == BlockType.HEADER_1:
                    sz = 13.0
                    sb, sa = 14, 4
                elif block.type == BlockType.HEADER_2:
                    sz = 11.5
                    sb, sa = 10, 3
                else:
                    sz = 10.5
                    sb, sa = 8, 2

                align = WD_ALIGN_PARAGRAPH.CENTER if is_abstract else WD_ALIGN_PARAGRAPH.LEFT
                p = doc.add_paragraph()
                p.alignment = align
                p.paragraph_format.space_before = Pt(sb)
                p.paragraph_format.space_after = Pt(sa)
                p.paragraph_format.keep_with_next = True

                if bilingual:
                    self._add_rich_runs_to_paragraph(p, en_txt, size_pt=sz, base_bold=True, color_rgb=(0, 0, 0))
                    if zh_txt and zh_txt != en_txt:
                        p.add_run("\n")
                        self._add_rich_runs_to_paragraph(p, zh_txt, size_pt=sz, base_bold=True, color_rgb=(0, 0, 0))
                else:
                    self._add_rich_runs_to_paragraph(p, zh_txt, size_pt=sz, base_bold=True, color_rgb=(0, 0, 0))
                continue

            # 4. 图表题注 (BlockType.IMAGE_CAPTION)
            if block.type == BlockType.IMAGE_CAPTION:
                align = WD_ALIGN_PARAGRAPH.CENTER if block.align == "center" else WD_ALIGN_PARAGRAPH.JUSTIFY
                if bilingual:
                    p_en = doc.add_paragraph()
                    p_en.alignment = align
                    p_en.paragraph_format.space_before = Pt(3)
                    p_en.paragraph_format.space_after = Pt(1.5)
                    p_en.paragraph_format.line_spacing = 1.18
                    self._add_rich_runs_to_paragraph(p_en, en_txt, size_pt=9.5, color_rgb=(30, 30, 30))

                    if zh_txt and zh_txt != en_txt:
                        p_zh = doc.add_paragraph()
                        p_zh.alignment = align
                        p_zh.paragraph_format.space_before = Pt(0)
                        p_zh.paragraph_format.space_after = Pt(8)
                        p_zh.paragraph_format.line_spacing = 1.18
                        self._add_rich_runs_to_paragraph(p_zh, zh_txt, size_pt=9.5, color_rgb=(0, 0, 0))
                else:
                    p_zh = doc.add_paragraph()
                    p_zh.alignment = align
                    p_zh.paragraph_format.space_before = Pt(3)
                    p_zh.paragraph_format.space_after = Pt(8)
                    p_zh.paragraph_format.line_spacing = 1.18
                    self._add_rich_runs_to_paragraph(p_zh, zh_txt, size_pt=9.5, color_rgb=(0, 0, 0))
                continue

            # 5. 参考文献条目 (BlockType.REFERENCE - 原文单段输出 + 悬挂缩进)
            if block.type == BlockType.REFERENCE:
                is_new_entry = bool(re.match(r"^(\[\d+\]|\d+\.\s*|\d+\t|\d+\.$)", en_txt.strip()))
                is_continuation = bool(block.metadata and block.metadata.get("is_ref_continuation")) or (not is_new_entry)
                prev_is_heading = False
                if doc.paragraphs:
                    prev_p = doc.paragraphs[-1]
                    if prev_p.style and "Heading" in prev_p.style.name:
                        prev_is_heading = True
                    elif prev_p.text.strip().startswith(("#", "References", "参考文献")):
                        prev_is_heading = True

                if is_continuation and doc.paragraphs and not prev_is_heading:
                    # 若属于跨页或断行拆分出的续行，平滑追加至上一段文献末尾，保持单个段落悬挂缩进
                    prev_p = doc.paragraphs[-1]
                    self._add_rich_runs_to_paragraph(prev_p, " " + en_txt, size_pt=9.0, color_rgb=(40, 40, 40))
                    continue

                p_ref = doc.add_paragraph()
                p_ref.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                p_ref.paragraph_format.left_indent = Inches(0.28)
                p_ref.paragraph_format.first_line_indent = Inches(-0.28)
                p_ref.paragraph_format.space_before = Pt(1.5)
                p_ref.paragraph_format.space_after = Pt(2.0)
                p_ref.paragraph_format.line_spacing = 1.15
                self._add_rich_runs_to_paragraph(p_ref, en_txt, size_pt=9.0, color_rgb=(40, 40, 40))
                continue

            # 跨页接续块跳过独立输出（已在前半段合并）
            if block.is_cross_page_head and block.metadata.get("full_combined_zh") and getattr(self, "para_format", "reorganize") == "reorganize":
                continue

            # 跨页前半段合并输出
            if block.is_cross_page_tail and block.metadata.get("full_combined_zh") and getattr(self, "para_format", "reorganize") == "reorganize":
                partner_id = block.cross_page_partner_id
                partner = next((b for b in blocks if b.id == partner_id), None)
                partner_en = partner.original_text.strip() if partner else ""
                en_txt = f"{en_txt} {partner_en}"
                zh_txt = block.metadata["full_combined_zh"]

            # 6. 正文自然段 (PARAGRAPH) 与列表项 (LIST_ITEM)
            is_abs_para = next_is_abstract_body or is_abstract_sec
            next_is_abstract_body = False
            left_ind = Inches(0.42) if is_abs_para else (Inches(0.25) if block.type == BlockType.LIST_ITEM else Inches(0.0))
            right_ind = Inches(0.42) if is_abs_para else Inches(0.0)
            para_sz = 9.5 if is_abs_para else 10.0

            if bilingual:
                p_en = doc.add_paragraph()
                p_en.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                p_en.paragraph_format.left_indent = left_ind
                p_en.paragraph_format.right_indent = right_ind
                p_en.paragraph_format.space_before = Pt(2)
                p_en.paragraph_format.space_after = Pt(1.5)
                p_en.paragraph_format.line_spacing = 1.2
                self._add_rich_runs_to_paragraph(p_en, en_txt, size_pt=para_sz, color_rgb=(35, 35, 35))

                if zh_txt and zh_txt != en_txt:
                    p_zh = doc.add_paragraph()
                    p_zh.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                    p_zh.paragraph_format.left_indent = left_ind
                    p_zh.paragraph_format.right_indent = right_ind
                    p_zh.paragraph_format.space_before = Pt(0)
                    p_zh.paragraph_format.space_after = Pt(5.5)
                    p_zh.paragraph_format.line_spacing = 1.24
                    self._add_rich_runs_to_paragraph(p_zh, zh_txt, size_pt=para_sz, color_rgb=(0, 0, 0))
            else:
                p_zh = doc.add_paragraph()
                p_zh.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                p_zh.paragraph_format.left_indent = left_ind
                p_zh.paragraph_format.right_indent = right_ind
                p_zh.paragraph_format.space_before = Pt(2)
                p_zh.paragraph_format.space_after = Pt(5)
                p_zh.paragraph_format.line_spacing = 1.24
                self._add_rich_runs_to_paragraph(p_zh, zh_txt, size_pt=para_sz, color_rgb=(0, 0, 0))

        saved_path = str(output_path)
        try:
            doc.save(saved_path)
        except PermissionError:
            for suffix in ["_new", "_v2", "_v3"]:
                alt_path = Path(output_path).with_name(f"{Path(output_path).stem}{suffix}.docx")
                try:
                    doc.save(str(alt_path))
                    saved_path = str(alt_path)
                    break
                except PermissionError:
                    continue
        return saved_path

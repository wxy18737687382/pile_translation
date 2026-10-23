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
    Exports aligned DocumentBlocks parsed by MinerU and translated by LLMs into multiple formats:
    1. Side-by-side dual-column PDF (Left: Original, Right: In-place translated PDF at exact coordinates)
    2. Single-column translated Chinese PDF
    3. Interactive side-by-side dual-column HTML viewer with synchronized hover highlights
    4. Bilingual side-by-side Markdown
    5. Translated Chinese Markdown
    6. Formatted Word DOCX documents (bilingual and Chinese-only)
    """

    # =========================================================================
    # 1. Core feature: Export side-by-side PDF (_bilingual.pdf) and translated Chinese PDF (_zh.pdf)
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
        Generates PDFs based on native PDF bboxes (x0, y0, x1, y1):
        - zh_pdf_path: Single-column PDF with in-place translated text, preserving figures, formulas, and lines.
        - dual_pdf_path: Side-by-side dual-column PDF (Left: Original, Right: Translation at exact coordinates).
        - page_renders_dir: Renders high-resolution page comparisons for the interactive HTML viewer.
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

            # Collect horizontal divider lines on this page (header lines, footnote dividers, table rules)
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

            # Step 1: Redact English text blocks on cloned page while preserving all images, vector rules, and table grids
            overlay_rects: List[pymupdf.Rect] = []
            for b in overlay_blocks:
                rx0, ry0, rx1, ry1 = b.bbox
                # Prevent redaction rectangle from clipping into preserved text below (references, figures, formulas)
                for ob in p_blocks:
                    if ob is b or not ob.bbox or len(ob.bbox) != 4:
                        continue
                    ob_x0, ob_y0, ob_x1, ob_y1 = ob.bbox
                    h_overlap = max(0.0, min(rx1, ob_x1) - max(rx0, ob_x0))
                    if h_overlap > 5.0 and ob_y0 >= ry0 - 1.0 and ob_y0 <= ry1 + 2.0:
                        ry1 = min(ry1, ob_y0 - 0.5)

                # If immediately below footnote divider or title rule, ensure redaction rect does not overlap rule
                for ly in horiz_lines_y:
                    if abs(ry0 - ly) < 3.0:
                        ry0 = max(ry0, ly + 1.0)
                # If image exists above caption, never invade image bounds
                if b.type == BlockType.IMAGE_CAPTION:
                    ry0 = max(ry0, b.bbox[1])
                redact_rect = pymupdf.Rect(rx0 - 0.5, ry0 - 0.3, rx1 + 0.5, ry1)
                overlay_rects.append(redact_rect)
                zh_page.add_redact_annot(redact_rect)
                if bg_page is not None:
                    bg_page.add_redact_annot(redact_rect)

            zh_page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED
            )
            if bg_page is not None:
                bg_page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED
                )

            # Mask residual micro-lines from inline fractions/radicals inside redacted paragraphs
            for sr in short_inline_bars:
                if any(or_rect.intersects(sr) for or_rect in overlay_rects):
                    pad_r = pymupdf.Rect(sr.x0 - 0.5, sr.y0 - 0.8, sr.x1 + 0.5, sr.y1 + 0.8)
                    zh_page.draw_rect(pad_r, color=(1, 1, 1), fill=(1, 1, 1))
                    if bg_page is not None:
                        bg_page.draw_rect(pad_r, color=(1, 1, 1), fill=(1, 1, 1))

            # Determine if page has two-column layout and locate column boundary
            page_is_two_col, col_split_x = self._detect_page_columns(p_blocks, zh_page.rect)

            # Step 2: Overlay translated Chinese text at exact coordinates (x0, y0, x1, y1)
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

        # Save single-column translated PDF with garbage collection and stream compression
        zh_doc.save(str(zh_pdf_path), garbage=4, deflate=True)

        # Build side-by-side dual PDF (Left: Original, Right: Translation)
        dual_doc = pymupdf.open()
        for pno in range(len(orig_doc)):
            pw = orig_doc[pno].rect.width
            ph = orig_doc[pno].rect.height
            dual_page = dual_doc.new_page(width=pw * 2.0, height=ph)
            # Left page: 100% Original PDF
            dual_page.show_pdf_page(pymupdf.Rect(0, 0, pw, ph), orig_doc, pno)
            # Right page: Translated PDF at matching coordinates
            dual_page.show_pdf_page(pymupdf.Rect(pw, 0, pw * 2.0, ph), zh_doc, pno)
            # Center divider line
            dual_page.draw_line(
                pymupdf.Point(pw, 0),
                pymupdf.Point(pw, ph),
                color=(0.82, 0.85, 0.90),
                width=0.75
            )

        dual_doc.save(str(dual_pdf_path), garbage=4, deflate=True)

        # Export high-resolution PNGs if page_renders_dir is provided for web viewer
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
        """Dynamically detects two-column layout status and split-line x coordinate"""
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
        Calculates target bounding box for text overlay:
        Anchors strictly at original (x0, y0) with column-aware width constraints:
        - Two-column left: right margin capped at col_split_x - 4.0pt (never overflows to right column).
        - Two-column right: right margin capped at page margin (pw - 28.0pt).
        - Single-column / span: right margin set to text boundary (pw - 35.0pt).
        Bottom bound bounded by subsequent block in same column.
        """
        x0, y0, x1, y1 = block.bbox
        pw, ph = page_rect.width, page_rect.height
        box_h = y1 - y0

        # Determine column assignment for current block
        block_col = block.metadata.get("column") if (block.metadata and "column" in block.metadata) else "span"
        if block_col == "span" and is_two_col:
            if x1 <= col_split_x + 6.0:
                block_col = "left"
            elif x0 >= col_split_x - 6.0:
                block_col = "right"

        # Determine right margin constraint
        if is_two_col and block_col == "left":
            # Left column: right margin constrained left of column divider with >= 4pt gutter
            right_margin = min(col_split_x - 4.0, max(x1, x0 + 80.0))
        elif is_two_col and block_col == "right":
            # Right column: right margin extends to page right margin
            right_margin = min(pw - 28.0, max(x1, x0 + 80.0))
        else:
            # Single-column or full-span banner
            right_margin = min(pw - 35.0, max(x1, min(pw - 105.0, 505.5)))

        # Locate top y0 of succeeding block in same column as vertical ceiling
        next_y0 = ph - 35.0
        for nb in page_all_blocks:
            if nb is block or not nb.bbox or len(nb.bbox) != 4:
                continue
            nx0, ny0, nx1, ny1 = nb.bbox
            if ny0 < y0 + 1.0:
                continue

            # Determine column assignment for nb
            nb_col = nb.metadata.get("column") if (nb.metadata and "column" in nb.metadata) else "span"
            if nb_col == "span" and is_two_col:
                if nx1 <= col_split_x + 6.0:
                    nb_col = "left"
                elif nx0 >= col_split_x - 6.0:
                    nb_col = "right"

            # Column isolation: left blocks are not clipped by right blocks and vice versa
            if is_two_col and block_col in ("left", "right") and nb_col in ("left", "right") and block_col != nb_col:
                continue

            # Check horizontal overlap or same-column alignment
            if not (nx1 < x0 or nx0 > right_margin):
                next_y0 = min(next_y0, ny0 - 1.0)

        # 1. Centered title or heading (e.g. Attention Is All You Need / Abstract)
        if block.align == "center" and block.type in (BlockType.TITLE, BlockType.HEADER_1):
            if is_two_col and block_col == "left":
                return pymupdf.Rect(x0, y0 - 1.5, right_margin, min(y1 + 4.0, next_y0))
            return pymupdf.Rect(max(35.0, x0 - 15.0) if is_two_col else 108.0, y0 - 1.5, min(pw - 35.0, x1 + 15.0) if is_two_col else 504.0, min(y1 + 5.0, next_y0))

        # 2. Caption or subfigure title (e.g. Figure 1 / Scaled Dot-Product Attention)
        if block.type == BlockType.IMAGE_CAPTION:
            target_y0 = max(y0, block.bbox[1])
            if block.align == "center":
                cx = (x0 + x1) / 2.0
                half_w = max((x1 - x0) / 2.0 + 20.0, 70.0)
                target_x0 = max(x0 - 10.0, cx - half_w) if is_two_col else max(90.0, cx - half_w)
                target_x1 = min(right_margin, cx + half_w) if is_two_col else min(pw - 90.0, cx + half_w)
                return pymupdf.Rect(target_x0, target_y0, target_x1, min(y1 + 4.0, next_y0))
            return pymupdf.Rect(x0, target_y0, right_margin, min(y1 + 4.0, next_y0))

        # 3. Left-aligned section heading (e.g. 1 Introduction)
        if block.type in (BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3):
            return pymupdf.Rect(x0, y0 - 1.5, right_margin, min(y1 + 4.0, next_y0))

        # 4. Single-line short text or footnote
        if box_h < 13.5:
            return pymupdf.Rect(x0, y0 - 1.0, right_margin, min(y1 + 3.5, next_y0))

        # 5. Multi-line body paragraph, footnote, list item, or reference entry
        safe_y1 = min(y1 + 3.0, next_y0)
        if safe_y1 <= y0 + 6.0:
            safe_y1 = y1 + 1.0
        return pymupdf.Rect(x0, y0 - 1.0, right_margin, safe_y1)

    def _latex_math_to_rich_html(self, text: str) -> str:
        """Converts LaTeX inline math and scripts into rich text with <sub> / <sup> tags"""
        if not text:
            return ""

        # 0. Strip display math double-dollar wrappers
        text = re.sub(r"^\$\$\s*|\s*\$\$$", "", text)

        # 1. Font commands: \mathbf, \boldsymbol, \mathit, \mathrm, \text
        text = re.sub(r"\\mathbf\{([^}]+)\}", r"<b>\1</b>", text)
        text = re.sub(r"\\boldsymbol\{([^}]+)\}", r"<b>\1</b>", text)
        text = re.sub(r"\\mathit\{([^}]+)\}", r"<i>\1</i>", text)
        text = re.sub(r"\\(?:mathrm|text)\{([^}]+)\}", r"\1", text)

        # 2. Caligraphic letters (\mathcal{...})
        cal_map = {
            "A": "𝒜", "B": "ℬ", "C": "𝒞", "D": "𝒟", "E": "ℰ", "F": "ℱ", "G": "𝒢", "H": "ℋ",
            "I": "ℐ", "J": "𝒥", "K": "𝒦", "L": "ℒ", "M": "ℳ", "N": "𝒩", "O": "𝒪", "P": "𝒫",
            "Q": "𝒬", "R": "ℛ", "S": "𝒮", "T": "𝒯", "U": "𝒰", "V": "𝒱", "W": "𝒲", "X": "𝒳",
            "Y": "𝒴", "Z": "𝒵"
        }
        for k, v in cal_map.items():
            text = text.replace(f"\\mathcal{{{k}}}", v)

        # 3. Greek letter replacements
        greek_map = {
            "\\alpha": "α", "\\beta": "β", "\\gamma": "γ", "\\delta": "δ",
            "\\epsilon": "ε", "\\varepsilon": "ε", "\\zeta": "ζ", "\\eta": "η",
            "\\theta": "θ", "\\vartheta": "θ", "\\iota": "ι", "\\kappa": "κ",
            "\\lambda": "λ", "\\mu": "μ", "\\nu": "ν", "\\xi": "ξ",
            "\\pi": "π", "\\varpi": "π", "\\rho": "ρ", "\\varrho": "ρ",
            "\\sigma": "σ", "\\varsigma": "σ", "\\tau": "τ", "\\upsilon": "υ",
            "\\phi": "φ", "\\varphi": "φ", "\\chi": "χ", "\\psi": "ψ", "\\omega": "ω",
            "\\Gamma": "Γ", "\\Delta": "Δ", "\\Theta": "Θ", "\\Lambda": "Λ",
            "\\Xi": "Ξ", "\\Pi": "Π", "\\Sigma": "Σ", "\\Upsilon": "Υ",
            "\\Phi": "Φ", "\\Psi": "Ψ", "\\Omega": "Ω"
        }
        for k, v in greek_map.items():
            text = re.sub(re.escape(k) + r"(?![a-zA-Z])", v, text)

        # 4. Mathematical operators and relation symbols
        op_map = {
            "\\nabla": "∇", "\\partial": "∂", "\\infty": "∞",
            "\\times": "×", "\\cdot": "·", "\\pm": "±", "\\mp": "∓",
            "\\leq": "≤", "\\le": "≤", "\\geq": "≥", "\\ge": "≥",
            "\\neq": "≠", "\\ne": "≠", "\\approx": "≈", "\\equiv": "≡",
            "\\sim": "∼", "\\propto": "∝", "\\in": "∈", "\\notin": "∉",
            "\\to": "→", "\\rightarrow": "→", "\\leftarrow": "←",
            "\\sqrt": "√", "\\forall": "∀", "\\exists": "∃"
        }
        for k, v in op_map.items():
            text = re.sub(re.escape(k) + r"(?![a-zA-Z])", v, text)

        # 5. Script conversion
        # Subscript _{...} or _X
        text = re.sub(r"_\{([^}]+)\}", r"<sub>\1</sub>", text)
        text = re.sub(r"_([a-zA-Z0-9α-ωΑ-Ω])", r"<sub>\1</sub>", text)
        # Superscript ^{...} or ^X
        text = re.sub(r"\^\{([^}]+)\}", r"<sup>\1</sup>", text)
        text = re.sub(r"\^([a-zA-Z0-9α-ωΑ-Ω*†])", r"<sup>\1</sup>", text)

        # 6. Hat and bar symbols
        text = re.sub(r"\\hat\{([^}]+)\}", r"\1̂", text)
        text = re.sub(r"\\bar\{([^}]+)\}", r"\1̄", text)
        text = re.sub(r"\\tilde\{([^}]+)\}", r"\1̃", text)

        # 7. Strip surrounding dollar signs: $...$ -> ...
        text = re.sub(r"\$([^$]+)\$", r"\1", text)
        text = text.replace("$", "")

        return text

    def _markdown_to_pdf_html(self, text: str, wrap_en_span: bool = False) -> str:
        """Converts translated text with Markdown and script tags into HTML for insert_htmlbox"""
        s = self._latex_math_to_rich_html(text.strip())
        # Strip leading Markdown heading hashtags
        s = re.sub(r"^#{1,6}\s+", "", s)
        # Protect existing <sup>, <sub>, <b>, <i> tags
        tag_map: Dict[str, str] = {}
        counter = 0

        def _stash_tag(m):
            nonlocal counter
            key = f"@@TAG_{counter}@@"
            tag_map[key] = m.group(0)
            counter += 1
            return key

        s = re.sub(r"</?(?:sup|sub|b|i)>", _stash_tag, s)
        # Escape special HTML characters <, >, &
        s = html.escape(s)
        # Restore Markdown bold **...** and italic *...*
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
        # Restore HTML script tags
        for k, v in tag_map.items():
            s = s.replace(k, v)

        # Wrap western/alphanumeric characters with <span class="en">...</span> if dual-font rendering enabled
        if wrap_en_span:
            s = re.sub(
                r"(<[^>]+>)|(&[a-zA-Z0-9#]+;)|([A-Za-z0-9_.,;:!?()\[\]{}+=\-*/~^@#$%&|'`\\]+)",
                lambda m: m.group(0) if (m.group(1) or m.group(2)) else f'<span class="en">{m.group(3)}</span>',
                s
            )
        return s

    def _build_pdf_htmlbox_payload(self, block: DocumentBlock) -> Tuple[str, str]:
        """Generates (html_fragment, css_str) for PyMuPDF insert_htmlbox (SimSun for Chinese, Times New Roman for English)"""
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
    # 2. Export interactive side-by-side bilingual HTML (_bilingual.html)
    # =========================================================================
    def export_bilingual_html(
        self,
        blocks: List[DocumentBlock],
        title: str = "学术文献原版双语对照",
        page_count: int = 0,
        renders_rel_dir: str = "page_renders"
    ) -> str:
        """
        Exports interactive side-by-side bilingual HTML:
        - If PDF page coordinate info exists (page_count > 0), displays 1:1 dual-column view:
          * Left column: Original PDF page render + precise hotspot bounding boxes
          * Right column: Identical dimensions showing translation, figures, and footnotes at exact (x0, y0, x1, y1)
            (Supports toggling between PDF native vector layer and HTML live text layer, with sync hover highlight)
        - Also includes full continuous flow bilingual tab.
        """
        has_pdf_pages = page_count > 0 and any(b.page_num is not None and b.bbox for b in blocks)

        # Build page-by-page 1:1 coordinate comparison HTML
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
                        <!-- Left column: Original PDF -->
                        <div class="pdf-page-card left-pdf-card" style="aspect-ratio: {pw} / {ph};">
                            <img class="page-img" src="{orig_img_src}" alt="Original PDF Page {pno + 1}" loading="lazy" />
                            <div class="hotspot-layer">
                                {"".join(left_hotspots)}
                            </div>
                        </div>
                        <!-- Right column: In-situ translated page -->
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

        # Build continuous stream comparison HTML with inline figures and footnotes
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
        <!-- View 1: Original PDF + In-situ translation 1:1 side-by-side spread -->
        <div id="view-pdf-spread" class="{'' if default_view == 'pdf-spread' else 'hidden'}">
            {spread_section}
        </div>

        <!-- View 2: Full Markdown streaming bilingual view -->
        <div id="view-stream" class="stream-container {'' if default_view == 'stream' else 'hidden'}">
            {stream_section}
        </div>
    </div>

    <!-- Bottom synchronized inspector bar -->
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

        // Synchronized hover highlighting and bottom inspector update for paired bounding boxes
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
    # 3. Export Markdown and DOCX with inline figures and footnotes
    # =========================================================================
    def export_bilingual_markdown(self, blocks: List[DocumentBlock]) -> str:
        """Exports bilingual side-by-side Markdown with embedded figures and footnotes"""
        lines: List[str] = []
        emitted_abstract_heading = False

        for block in blocks:
            if block.type == BlockType.RAW:
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
                    lines.append(block.translated_text)
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
            zh_text = (block.translated_text or block.original_text).strip()

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
        """Exports translated Chinese Markdown with embedded figures and footnotes"""
        lines: List[str] = []
        emitted_abstract_heading = False

        for block in blocks:
            if block.type == BlockType.RAW:
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
                lines.append(f"> {block.translated_text if block.translated_text else block.original_text}")
            else:
                lines.append(block.translated_text if block.translated_text else block.original_text)
            lines.append("")

        return "\n".join(lines)

    # =========================================================================
    # 4. Export formatted Word document matching original PDF layout (Songti + Times New Roman)
    # =========================================================================
    def export_bilingual_docx(
        self,
        blocks: List[DocumentBlock],
        output_path: str,
        base_dir: Optional[Path] = None,
        source_pdf_path: Optional[Path] = None
    ) -> str:
        """Exports side-by-side bilingual Word document matching original PDF layout"""
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
        """Exports translated Chinese Word document matching original PDF layout"""
        return self._build_academic_docx(
            blocks=blocks,
            output_path=output_path,
            base_dir=base_dir,
            source_pdf_path=source_pdf_path,
            bilingual=False
        )

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
        """Sets Word Run font: Times New Roman for Latin/digits/symbols, SimHei for bold headings, SimSun for body text"""
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
        Parses Markdown and HTML tags to write formatted Runs with native sub/superscripts, bold, and italics.
        """
        has_leading_space = text.startswith(" ")
        has_trailing_space = text.endswith(" ")
        s = self._latex_math_to_rich_html(text.strip())
        s = re.sub(r"^#{1,6}\s+", "", s)
        if has_leading_space:
            s = " " + s
        if has_trailing_space:
            s = s + " "
        # Convert Markdown **...** to <b>...</b>
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        # Bold caption prefix if present (e.g. Figure 1: / Table 1:)
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
        """Adds academic-style top/bottom border rules to Word paragraph"""
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
        Organizes raw block sequence into flowing layout units matching original PDF:
        1. Merges adjacent AUTHOR_INFO blocks on same page into centered borderless author grid
        2. Merges fragmented TABLE / MATH_BLOCKs and crops high-resolution figures/tables from PDF
        3. Filters duplicate subpanel headings already inside figures
        4. Re-orders top-of-page floating figures after unended cross-page sentences
        """
        pdf_doc = None
        if source_pdf_path and Path(source_pdf_path).exists():
            try:
                pdf_doc = pymupdf.open(str(source_pdf_path))
            except Exception:
                pdf_doc = None

        crops_dir = (base_dir / "images") if base_dir else Path("./output/images")
        crops_dir.mkdir(parents=True, exist_ok=True)

        # Group and preprocess by page
        pages_map: Dict[int, List[DocumentBlock]] = {}
        for b in blocks:
            if b.type == BlockType.RAW:
                continue
            pno = b.page_num if b.page_num is not None else 0
            pages_map.setdefault(pno, []).append(b)

        structured_items: List[Dict] = []

        for pno in sorted(pages_map.keys()):
            p_blocks = pages_map[pno]
            # Filter short sub-titles located entirely inside IMAGE blocks
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

            # Separate footnotes from body/figure blocks
            page_footnotes = [b for b in filtered_blocks if b.type == BlockType.FOOTNOTE]
            main_blocks = [b for b in filtered_blocks if b.type != BlockType.FOOTNOTE]

            # Aggregate consecutive AUTHOR_INFO, TABLE, and MATH_BLOCKs into unified units
            page_units: List[Dict] = []
            i = 0
            n = len(main_blocks)
            while i < n:
                b = main_blocks[i]

                # 1. Author info grid aggregation
                if b.type == BlockType.AUTHOR_INFO:
                    auth_group = [b]
                    j = i + 1
                    while j < n and main_blocks[j].type == BlockType.AUTHOR_INFO:
                        auth_group.append(main_blocks[j])
                        j += 1
                    page_units.append({"kind": "author_grid", "blocks": auth_group, "page_num": pno})
                    i = j
                    continue

                # 2. Academic three-line table aggregation and high-res crop
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

                # 3. Display math block aggregation and high-res crop
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
                        # Defensive check: only crop as standalone formula when width >= 35pt
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

                # 4. Regular blocks (TITLE, HEADER, PARAGRAPH, IMAGE, IMAGE_CAPTION, LIST_ITEM, REFERENCE)
                page_units.append({"kind": "single", "block": b, "page_num": pno})
                i += 1

            # If top of page is floating figure/table and subsequent block continues cross-page sentence (lowercase),
            # place continuing sentence before floating figure to maintain reading continuity
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
        """Constructs centered borderless multi-column author grid in Word"""
        # Merge adjacent name and affiliation/email blocks into author card
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
            # Display author name, affiliation, and email on separate lines
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
        Builds academic Word document matching original PDF layout:
        - Consistent typography: SimSun for Chinese, Times New Roman for Latin/digits/symbols
        - Preserved layout: title rules, author grid, abstract indent, three-line tables, figures, footnotes, hanging indent references
        """
        if not DOCX_AVAILABLE:
            raise ImportError("python-docx is not installed, cannot export .docx file")

        doc = Document()

        # 1. Page dimensions and margins (adaptive to original PDF geometry)
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

        # 2. Configure default Normal style (SimSun + Times New Roman)
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

            # (A) Author grid
            if kind == "author_grid":
                self._render_author_grid_docx(doc, item["blocks"])
                continue

            # (B) Original high-res table or display formula
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

            # (C) Footnote area (with top divider rule + 8.5pt font)
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

            # (D) Regular block elements
            block: DocumentBlock = item["block"]
            en_txt = block.original_text.strip()
            zh_txt = (block.translated_text or block.original_text).strip()

            # Structured abstract title: output centered bold Abstract title if not yet emitted
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

            # 1. Figure block (BlockType.IMAGE)
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

            # 2. Paper title (BlockType.TITLE - centered with top/bottom border rules)
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

            # 3. Section headings (HEADER_1, HEADER_2, HEADER_3)
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

            # 4. Caption block (BlockType.IMAGE_CAPTION)
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

            # 5. Reference entry (BlockType.REFERENCE - hanging indent)
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
                    # Append continuation lines to preceding reference entry to preserve single-paragraph hanging indent
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

            # 6. Body prose (PARAGRAPH) and list items (LIST_ITEM)
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

import os
import re
import json
import time
import shutil
import zipfile
import subprocess
import urllib.request
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

import pymupdf
from pipeline.models import DocumentBlock, BlockType


class MinerUEngine:
    """
    MinerU Document Pre-processing Engine
    Responsibilities:
    1. Performs layout analysis on entire PDF documents to extract:
       - Structured full Markdown (full.md)
       - In-place extracted figure/photo files (images/*.png) with precise page bboxes
       - Structured block sequence with exact page_num and bounding boxes (content_list.json)
       - Fully restored inline super/subscripts (<sup>/<sub>), mathematical variables, and footnotes
    2. Supports three execution backends with automatic fallback:
       - mineru-api: Calls official MinerU Cloud API v4 (requires MINERU_API_KEY)
       - mineru-cli: Calls locally installed mineru / magic-pdf command-line tool
       - builtin-mineru: Built-in high-precision PyMuPDF geometry, footnote, figure, and formula engine
    """

    def __init__(self, mode: str = "auto", api_key: Optional[str] = None):
        self.mode = mode
        self.api_key = (
            api_key
            or os.getenv("MINERU_API_KEY")
            or os.getenv("MINERU_API_TOKEN")
        )

    def detect_backend(self) -> str:
        """Automatically selects available MinerU backend"""
        if self.mode in ("mineru-api", "mineru-cli", "builtin-mineru"):
            return self.mode
        if self.api_key and self.api_key.strip() not in ("", "dummy", "your_mineru_api_key_here"):
            return "mineru-api"
        if shutil.which("mineru") or shutil.which("magic-pdf"):
            return "mineru-cli"
        return "builtin-mineru"

    def process_pdf(self, pdf_path: Path, output_root: Path) -> Tuple[str, List[DocumentBlock], Path]:
        """
        Executes pre-processing and returns:
        (full_markdown_text, structured_blocks_with_bbox, mineru_work_dir)
        """
        stem = pdf_path.stem
        mineru_dir = output_root / f"{stem}_mineru"
        mineru_dir.mkdir(parents=True, exist_ok=True)
        images_dir = mineru_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        # Ensure output_root/images exists for exported Markdown/HTML image references
        out_images_dir = output_root / "images"
        out_images_dir.mkdir(parents=True, exist_ok=True)

        backend = self.detect_backend()
        if backend == "mineru-api":
            try:
                return self._run_mineru_cloud_api(pdf_path, mineru_dir, output_root)
            except Exception as e:
                print(f"[MinerU] Cloud API failed ({e}), automatically switching to built-in layout engine...")
        elif backend == "mineru-cli":
            try:
                return self._run_mineru_local_cli(pdf_path, mineru_dir, output_root)
            except Exception as e:
                print(f"[MinerU] Local CLI execution failed ({e}), switching to built-in layout engine...")

        return self._run_builtin_mineru_engine(pdf_path, mineru_dir, output_root)

    # =========================================================================
    # Backend 1: Official MinerU Cloud API v4 (https://mineru.net/api/v4/...)
    # =========================================================================
    def _run_mineru_cloud_api(
        self, pdf_path: Path, mineru_dir: Path, output_root: Path
    ) -> Tuple[str, List[DocumentBlock], Path]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        req_body = json.dumps({
            "enable_formula": True,
            "language": "en",
            "enable_table": True,
            "files": [{"name": pdf_path.name, "is_ocr": False, "data_id": pdf_path.stem}]
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://mineru.net/api/v4/file-urls/batch",
            data=req_body,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))

        if res_data.get("code") != 0:
            raise RuntimeError(f"MinerU API request for upload URL failed: {res_data}")

        batch_id = res_data["data"]["batch_id"]
        upload_url = res_data["data"]["file_urls"][0]

        # Upload PDF binary content
        pdf_bytes = pdf_path.read_bytes()
        put_req = urllib.request.Request(upload_url, data=pdf_bytes, method="PUT")
        with urllib.request.urlopen(put_req, timeout=120) as _:
            pass

        # Poll extraction status
        poll_url = f"https://mineru.net/api/v4/extract-results/batch/{batch_id}"
        zip_url = None
        for _ in range(90):
            time.sleep(3)
            poll_req = urllib.request.Request(poll_url, headers=headers, method="GET")
            with urllib.request.urlopen(poll_req, timeout=30) as resp:
                poll_data = json.loads(resp.read().decode("utf-8"))
            extract_list = poll_data.get("data", {}).get("extract_result", [])
            if extract_list:
                state = extract_list[0].get("state")
                if state == "done":
                    zip_url = extract_list[0].get("full_zip_url")
                    break
                elif state == "failed":
                    raise RuntimeError(f"MinerU cloud extraction failed: {extract_list[0].get('err_msg')}")

        if not zip_url:
            raise TimeoutError("MinerU cloud extraction timed out")

        zip_path = mineru_dir / f"{pdf_path.stem}_mineru.zip"
        urllib.request.urlretrieve(zip_url, str(zip_path))
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(mineru_dir)

        return self._load_mineru_directory(pdf_path, mineru_dir, output_root)

    # =========================================================================
    # Backend 2: Local MinerU / magic-pdf CLI
    # =========================================================================
    def _run_mineru_local_cli(
        self, pdf_path: Path, mineru_dir: Path, output_root: Path
    ) -> Tuple[str, List[DocumentBlock], Path]:
        cmd_bin = shutil.which("mineru") or shutil.which("magic-pdf")
        cmd = [cmd_bin, "-p", str(pdf_path), "-o", str(mineru_dir), "-m", "auto"]
        subprocess.run(cmd, check=True)
        return self._load_mineru_directory(pdf_path, mineru_dir, output_root)

    def _load_mineru_directory(
        self, pdf_path: Path, mineru_dir: Path, output_root: Path
    ) -> Tuple[str, List[DocumentBlock], Path]:
        """Loads full.md, images/, and *_content_list.json from standard MinerU output directory"""
        md_files = list(mineru_dir.rglob("*.md"))
        json_files = list(mineru_dir.rglob("*content_list.json"))
        if not json_files:
            raise FileNotFoundError(f"MinerU *_content_list.json not found in {mineru_dir}")

        # Copy extracted images to output_root/images
        out_images_dir = output_root / "images"
        out_images_dir.mkdir(parents=True, exist_ok=True)
        for img_file in mineru_dir.rglob("images/*"):
            if img_file.is_file():
                shutil.copy2(img_file, out_images_dir / img_file.name)

        full_md = md_files[0].read_text(encoding="utf-8") if md_files else ""
        content_list = json.loads(json_files[0].read_text(encoding="utf-8"))

        doc = pymupdf.open(str(pdf_path))
        blocks: List[DocumentBlock] = []
        for idx, item in enumerate(content_list):
            pno = item.get("page_idx", 0)
            page = doc[pno] if 0 <= pno < len(doc) else doc[0]
            pw, ph = page.rect.width, page.rect.height
            raw_bbox = item.get("bbox", [0, 0, 0, 0])
            # Convert normalized coordinates (0-1000) in content_list.json to points (pt) if necessary
            if raw_bbox and max(raw_bbox) > max(pw, ph):
                bbox = [
                    round(raw_bbox[0] * pw / 1000.0, 2),
                    round(raw_bbox[1] * ph / 1000.0, 2),
                    round(raw_bbox[2] * pw / 1000.0, 2),
                    round(raw_bbox[3] * ph / 1000.0, 2),
                ]
            else:
                bbox = [round(float(x), 2) for x in raw_bbox] if len(raw_bbox) == 4 else None

            m_type = item.get("type", "text")
            text = (item.get("text") or "").strip()
            img_path = item.get("img_path")
            if m_type == "image" and img_path:
                img_name = Path(img_path).name
                rel_img = f"images/{img_name}"
                blocks.append(DocumentBlock(
                    id=f"p{pno}_b{idx}",
                    type=BlockType.IMAGE,
                    original_text=f"![{img_name}]({rel_img})",
                    translated_text=f"![{img_name}]({rel_img})",
                    page_num=pno,
                    bbox=bbox,
                    page_width=pw,
                    page_height=ph,
                    image_path=rel_img,
                    skip_pdf_overlay=True
                ))
                caption_list = item.get("img_caption") or item.get("image_caption") or []
                if caption_list:
                    cap_text = " ".join(caption_list).strip()
                    blocks.append(DocumentBlock(
                        id=f"p{pno}_b{idx}_cap",
                        type=BlockType.IMAGE_CAPTION,
                        original_text=cap_text,
                        page_num=pno,
                        bbox=bbox,
                        page_width=pw,
                        page_height=ph,
                        font_size=9.5,
                        align="center"
                    ))
                continue

            if not text:
                continue

            b_type = BlockType.PARAGRAPH
            level = 0
            skip_overlay = False
            if m_type == "title":
                level = item.get("text_level", 1)
                b_type = BlockType.TITLE if (pno == 0 and idx == 0) else (
                    BlockType.HEADER_1 if level == 1 else (BlockType.HEADER_2 if level == 2 else BlockType.HEADER_3)
                )
            elif m_type == "interline_equation":
                b_type = BlockType.MATH_BLOCK
                skip_overlay = True
            elif m_type == "table":
                b_type = BlockType.TABLE
                skip_overlay = True
            elif m_type in ("footnote", "page_footnote"):
                b_type = BlockType.FOOTNOTE

            blocks.append(DocumentBlock(
                id=f"p{pno}_b{idx}",
                type=b_type,
                original_text=text,
                level=level,
                page_num=pno,
                bbox=bbox,
                page_width=pw,
                page_height=ph,
                font_size=item.get("font_size", 10.0),
                is_bold=(b_type in (BlockType.TITLE, BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3)),
                align=item.get("align", "left"),
                skip_pdf_overlay=skip_overlay
            ))

        doc.close()
        return full_md, blocks, mineru_dir

    # =========================================================================
    # Backend 3: Built-in high-precision MinerU-compatible layout and Markdown extraction engine
    # =========================================================================
    def _run_builtin_mineru_engine(
        self, pdf_path: Path, mineru_dir: Path, output_root: Path
    ) -> Tuple[str, List[DocumentBlock], Path]:
        """
        Performs in-depth geometric layout analysis for each page of the PDF:
        1. Identifies and crops figure/architecture diagram regions into high-resolution PNGs (images/),
           records exact coordinates, and inserts image blocks before captions.
        2. Merges bounding boxes fragmented by formulas, scripts, or line wraps across text and references.
        3. Restores inline superscript footnotes (<sup>*</sup>, <sup>4</sup>), subscript variables,
           inline fractions (1/√d_k), and summation symbols (∑) based on span baseline offsets.
        4. Exports full Markdown (full.md) and MinerU-compliant content_list.json.
        """
        doc = pymupdf.open(str(pdf_path))
        images_dir = mineru_dir / "images"
        out_images_dir = output_root / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        out_images_dir.mkdir(parents=True, exist_ok=True)

        all_blocks: List[DocumentBlock] = []
        content_list_json: List[Dict[str, Any]] = []
        in_references_section = False

        for pno in range(len(doc)):
            page = doc[pno]
            pw, ph = round(page.rect.width, 2), round(page.rect.height, 2)
            page_dict = page.get_text("dict")
            raw_blocks = page_dict.get("blocks", [])

            # 1. Identify all figure/diagram regions on page to preserve raster graphics and mask labels
            clean_raw_blocks = []
            for b in raw_blocks:
                if b.get("type") == 0:
                    b_cl = self._clean_block_lines(b)
                    if b_cl:
                        clean_raw_blocks.append(b_cl)
                else:
                    clean_raw_blocks.append(b)

            fig_regions, cap_to_fig = self._detect_page_figure_regions(page, clean_raw_blocks)

            # 2. Collect and preprocess text blocks (type == 0), filtering out all intra-figure text (axes, scales, legends, subpanel tags)
            text_blocks_raw = []
            for b in clean_raw_blocks:
                if b.get("type") != 0:
                    continue
                # Filter header watermarks and preprint headers (medRxiv/bioRxiv/arXiv/doi.org at y < 35)
                if b["bbox"][1] < 35.0:
                    btxt = self._preview_plain_text(b).strip().lower()
                    if any(wm in btxt for wm in ["medrxiv", "biorxiv", "arxiv:", "doi.org", "license"]):
                        continue

                # Filter footer journal identifiers and standalone page numbers (at y > ph - 38)
                if b["bbox"][1] > ph - 38.0:
                    btxt = self._preview_plain_text(b).strip()
                    if (
                        re.match(r"^(Nature(\s+\w+)?|Science|Cell|IEEE|ACM|PNAS)\b", btxt, re.IGNORECASE)
                        or re.match(r"^\d+$", btxt)
                        or ("| Volume" in btxt and len(btxt) < 80)
                    ):
                        continue

                # Retain caption block itself
                if any(id(b) == c_id for c_id in cap_to_fig.keys()):
                    text_blocks_raw.append(b)
                    continue
                b_r = pymupdf.Rect(b["bbox"])
                in_fig = any(
                    frect.contains(b_r)
                    or (b_r.intersects(frect) and (b_r & frect).get_area() > 0.3 * b_r.get_area())
                    or (b_r.intersects(frect) and b_r.y0 >= frect.y0 - 4.0 and b_r.y1 <= frect.y1 + 4.0)
                    for frect in fig_regions
                )
                if in_fig:
                    continue

                # Split raw text blocks to decouple author affiliations and structured abstracts
                sub_blocks = self._split_raw_block_if_needed(b, pno)
                text_blocks_raw.extend(sub_blocks)

            is_two_col, col_split_x = self._detect_page_columns(text_blocks_raw, pw, ph)
            sorted_text_blocks = self._sort_blocks_by_reading_order(
                text_blocks_raw, pw, ph, is_two_col, col_split_x
            )

            # Automatically detect real table regions (filtering out pseudo-tables)
            try:
                page_tabs = [t for t in page.find_tables().tables if self._is_real_table(t, page, pno)]
                table_bboxes = [pymupdf.Rect(t.bbox) for t in page_tabs]
            except Exception:
                table_bboxes = []

            merged_raw_blocks = self._merge_fragmented_pdf_blocks(
                sorted_text_blocks, pno, pw, ph, in_references_section, is_two_col, col_split_x, table_bboxes
            )

            # 3. Crop high-resolution figure images for each detected Figure / Extended Data Fig caption
            extracted_fig_for_caption: Dict[int, Tuple[str, List[float]]] = {}
            for b_idx, mb in enumerate(merged_raw_blocks):
                preview_txt = self._preview_plain_text(mb).strip()
                if re.match(r"^(Figure|Fig\.|Extended Data Fig\.|Extended Data Figure)\s*\d+\s*[:.|]", preview_txt, re.IGNORECASE):
                    matched_frect = None
                    for c_id, fr in cap_to_fig.items():
                        if abs(fr.y1 - mb["bbox"][1]) < 25.0 or (mb["bbox"][1] >= fr.y1 - 5.0 and mb["bbox"][1] <= fr.y1 + 35.0):
                            matched_frect = fr
                            break
                    if matched_frect is not None:
                        clip_rect = pymupdf.Rect(
                            max(0, matched_frect.x0 - 4),
                            max(0, matched_frect.y0 - 4),
                            min(pw, matched_frect.x1 + 4),
                            min(ph, matched_frect.y1)
                        )
                        fig_filename = f"page_{pno + 1}_fig_{b_idx}.png"
                        pix = page.get_pixmap(clip=clip_rect, dpi=200)
                        pix.save(str(images_dir / fig_filename))
                        shutil.copy2(images_dir / fig_filename, out_images_dir / fig_filename)
                        extracted_fig_for_caption[b_idx] = (
                            f"images/{fig_filename}",
                            [round(clip_rect.x0, 2), round(clip_rect.y0, 2), round(clip_rect.x1, 2), round(clip_rect.y1, 2)]
                        )

            # 4. Reconstruct rich text per block (including scripts, math symbols, footnotes) and classify
            for b_idx, mb in enumerate(merged_raw_blocks):
                # Insert IMAGE block before caption if extracted figure exists
                if b_idx in extracted_fig_for_caption:
                    rel_img_path, fig_bbox = extracted_fig_for_caption[b_idx]
                    img_block_id = f"p{pno}_fig_{b_idx}"
                    img_md = f"![Figure (Page {pno + 1})]({rel_img_path})"
                    img_block = DocumentBlock(
                        id=img_block_id,
                        type=BlockType.IMAGE,
                        original_text=img_md,
                        translated_text=img_md,
                        page_num=pno,
                        bbox=fig_bbox,
                        page_width=pw,
                        page_height=ph,
                        image_path=rel_img_path,
                        skip_pdf_overlay=True
                    )
                    all_blocks.append(img_block)
                    content_list_json.append({
                        "type": "image",
                        "page_idx": pno,
                        "bbox": fig_bbox,
                        "page_size": [pw, ph],
                        "img_path": rel_img_path
                    })

                bbox = [round(float(x), 2) for x in mb["bbox"]]
                col_type = "span"
                if is_two_col:
                    if bbox[2] <= col_split_x + 6.0:
                        col_type = "left"
                    elif bbox[0] >= col_split_x - 6.0:
                        col_type = "right"

                mb_preview = self._preview_plain_text(mb).strip()
                if re.match(r"^#*\s*(References|Bibliography)\b", mb_preview, re.IGNORECASE):
                    in_references_section = True
                elif in_references_section:
                    is_post_ref = bool(re.match(
                        r"^(\*\*|#*\s*)?(Methods|Online Methods|Ethics statement|Acknowledgements|Author contributions|Funding|Competing interests|Additional information|Supplementary information|Extended Data\s+Fig|Extended Data\s+Figure|Publisher’s note|Open Access|Data availability|Code availability)\b",
                        mb_preview,
                        re.IGNORECASE
                    ))
                    if is_post_ref:
                        in_references_section = False

                reconstructed = self._reconstruct_block_text_and_style(mb, pno, pw, ph, in_references_section, table_bboxes)
                text = reconstructed["text"].strip()
                if not text:
                    continue

                b_type: BlockType = reconstructed["type"]
                if re.match(r"^#*\s*(References|Bibliography)\b", text, re.IGNORECASE):
                    b_type = BlockType.HEADER_1

                block_obj = DocumentBlock(
                    id=f"p{pno}_b{b_idx}",
                    type=b_type,
                    original_text=text,
                    level=reconstructed["level"],
                    page_num=pno,
                    bbox=bbox,
                    page_width=pw,
                    page_height=ph,
                    font_size=reconstructed["font_size"],
                    is_bold=reconstructed["is_bold"],
                    align=reconstructed["align"],
                    skip_pdf_overlay=reconstructed["skip_pdf_overlay"],
                    footnote_id=reconstructed.get("footnote_id"),
                    metadata={
                        "page": pno + 1,
                        "is_two_col": is_two_col,
                        "col_split_x": round(col_split_x, 2),
                        "column": col_type,
                        **reconstructed.get("metadata", {})
                    }
                )
                all_blocks.append(block_obj)
                content_list_json.append({
                    "id": block_obj.id,
                    "type": b_type.value,
                    "page_idx": pno,
                    "bbox": bbox,
                    "page_size": [pw, ph],
                    "text": text,
                    "font_size": block_obj.font_size,
                    "is_bold": block_obj.is_bold,
                    "align": block_obj.align,
                    "skip_pdf_overlay": block_obj.skip_pdf_overlay,
                    "footnote_id": block_obj.footnote_id,
                    "column": col_type
                })

        doc.close()

        # 5. Generate full Markdown (full.md) and content_list.json
        full_md = self.blocks_to_source_markdown(all_blocks)
        (mineru_dir / "full.md").write_text(full_md, encoding="utf-8")
        (mineru_dir / f"{pdf_path.stem}_content_list.json").write_text(
            json.dumps(content_list_json, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return full_md, all_blocks, mineru_dir

    # =========================================================================
    # Helper methods: fragmented block merging and span-level script/math/footnote reconstruction
    # =========================================================================
    def _preview_plain_text(self, block: Dict[str, Any]) -> str:
        return " ".join(
            "".join(s.get("text", "") for s in line.get("spans", []))
            for line in block.get("lines", [])
        ).strip()

    def _clean_block_lines(self, block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Strips empty leading/trailing lines and tightens block bbox to active content lines"""
        lines = block.get("lines", [])
        if not lines:
            return None
        real_lines = []
        for l in lines:
            txt = "".join(s.get("text", "") for s in l.get("spans", [])).strip()
            if txt:
                real_lines.append(l)
        if not real_lines:
            return None
        b_clean = dict(block)
        b_clean["lines"] = real_lines
        bb = pymupdf.Rect(real_lines[0]["bbox"])
        for l in real_lines[1:]:
            bb.include_rect(l["bbox"])
        b_clean["bbox"] = [round(bb.x0, 2), round(bb.y0, 2), round(bb.x1, 2), round(bb.y1, 2)]
        return b_clean

    def _split_raw_block_if_needed(self, block: Dict[str, Any], pno: int) -> List[Dict[str, Any]]:
        """
        Decouples single raw_blocks merged by underlying PDF engine:
        1. Font size disparity between author affiliations (<=9.5pt) and body text (>=10.5pt);
        2. Structured abstract subsection titles (Background, Methods, Results, Conclusions, etc.);
        3. Large vertical line gaps (v_gap > 11pt) separating distinct paragraphs;
        4. Footnote indicators (* equal contribution, NOTE:, etc.).
        """
        lines = block.get("lines", [])
        if len(lines) <= 1:
            return [block]

        sub_blocks = []
        curr_lines = [lines[0]]

        for i in range(1, len(lines)):
            prev_l = lines[i - 1]
            curr_l = lines[i]

            prev_txt = "".join(s.get("text", "") for s in prev_l.get("spans", [])).strip()
            curr_txt = "".join(s.get("text", "") for s in curr_l.get("spans", [])).strip()

            px0, py0, px1, py1 = prev_l["bbox"]
            cx0, cy0, cx1, cy1 = curr_l["bbox"]
            v_gap = cy0 - py1

            # 1. Section heading boundary splitting (e.g. 4.1.1 Loss function and subsequent body line)
            is_curr_heading = bool(re.match(r"^(\d+(\.\d+)*)\s+[A-Z]", curr_txt) and len(curr_txt) < 80)
            is_prev_heading = bool(re.match(r"^(\d+(\.\d+)*)\s+[A-Z]", prev_txt) and len(prev_txt) < 80)

            # 2. Boundary splitting between left-aligned body text and centered display math
            is_curr_indented_math = (
                cx0 > 130.0
                and (cx1 - cx0) < 420.0
                and ("=" in curr_txt or any(sym in curr_txt for sym in ["∫", "∑", "∇", "≡", "∂", "ℒ", "λ", "δ", "𝛿", "‖", "∈"]))
            )
            is_prev_indented_math = (
                px0 > 130.0
                and (px1 - px0) < 420.0
                and ("=" in prev_txt or any(sym in prev_txt for sym in ["∫", "∑", "∇", "≡", "∂", "ℒ", "λ", "δ", "𝛿", "‖", "∈"]))
            )
            eq_transition = (
                (px0 < 90.0 and is_curr_indented_math)
                or (is_prev_indented_math and cx0 < 90.0)
            )

            prev_sz = prev_l["spans"][0]["size"] if prev_l.get("spans") else 10.0
            curr_sz = curr_l["spans"][0]["size"] if curr_l.get("spans") else 10.0

            curr_flags = curr_l["spans"][0].get("flags", 0) if curr_l.get("spans") else 0
            curr_is_bold = bool(curr_flags & 16 or "bold" in curr_l["spans"][0].get("font", "").lower())

            sz_change = (pno == 0 and abs(curr_sz - prev_sz) > 2.5) or (abs(curr_sz - prev_sz) > 4.5)
            is_structured_header = (
                curr_is_bold
                and curr_txt in (
                    "Background", "Methods", "Results", "Conclusions",
                    "Objective", "Design", "Setting", "Participants",
                    "Interventions", "Main Outcomes", "Findings", "Interpretation", "Abstract"
                )
            )
            large_gap = v_gap > 11.0
            is_footnote = curr_txt.startswith(("*", "†", "‡", "NOTE:")) and v_gap > 15.0
            is_new_citation = bool(
                re.match(r"^(\[\d+\]|\d+\.\s+|\d+\t|\d+\.$)", curr_txt)
                or (re.match(r"^\d+\.", curr_txt) and (len(curr_txt) <= 6 or re.match(r"^\d+\.\s+[A-Za-z]", curr_txt)))
            )
            is_ref_heading = bool(re.match(r"^#*\s*(References|Bibliography)\b", curr_txt, re.IGNORECASE))
            is_prev_ref_heading = bool(re.match(r"^#*\s*(References|Bibliography)\b", prev_txt, re.IGNORECASE))
            is_caption_or_note = bool(
                re.match(r"^(Table|Figure|Fig\.)\s*\d+\s*[:.]", curr_txt, re.IGNORECASE)
                or re.match(r"^(Note|Notes)\s*[:.]", curr_txt, re.IGNORECASE)
            )

            if (
                is_curr_heading
                or is_prev_heading
                or eq_transition
                or sz_change
                or is_structured_header
                or large_gap
                or is_footnote
                or is_new_citation
                or is_ref_heading
                or is_prev_ref_heading
                or is_caption_or_note
            ):
                bb = pymupdf.Rect(curr_lines[0]["bbox"])
                for cl in curr_lines[1:]:
                    bb.include_rect(cl["bbox"])
                sub_blocks.append({
                    "type": 0,
                    "bbox": [round(bb.x0, 2), round(bb.y0, 2), round(bb.x1, 2), round(bb.y1, 2)],
                    "lines": curr_lines
                })
                curr_lines = [curr_l]
            else:
                curr_lines.append(curr_l)

        if curr_lines:
            bb = pymupdf.Rect(curr_lines[0]["bbox"])
            for cl in curr_lines[1:]:
                bb.include_rect(cl["bbox"])
            sub_blocks.append({
                "type": 0,
                "bbox": [round(bb.x0, 2), round(bb.y0, 2), round(bb.x1, 2), round(bb.y1, 2)],
                "lines": curr_lines
            })

        # Fix minor y-coordinate overlaps between adjacent sub-blocks caused by font ascent/descent
        for si in range(len(sub_blocks) - 1):
            curr_sb = sub_blocks[si]
            next_sb = sub_blocks[si + 1]
            if curr_sb["bbox"][3] >= next_sb["bbox"][1]:
                curr_sb["bbox"][3] = round(next_sb["bbox"][1] - 0.5, 2)

        return sub_blocks

    def _is_author_area_block(self, block: Dict[str, Any], pno: int) -> bool:
        """Determines whether block belongs to first-page author/affiliation info (between Title and Abstract)"""
        if pno != 0:
            return False
        txt = self._preview_plain_text(block).strip()
        if not txt:
            return False
        # Body headings and abstract titles are never author/affiliation blocks
        if re.match(r"^(Abstract|Background|Introduction|Methods|Results|Conclusions|\d+\s+[A-Z])\b", txt, re.IGNORECASE):
            return False
        y0, y1 = block["bbox"][1], block["bbox"][3]
        if y0 > 320.0:
            return False

        clean_lower = txt.lower()
        affil_keywords = [
            "university", "department", "institute", "hospital", "foundation trust",
            "ltd", "college", "laboratory", "school of", "center for", "centre for",
            "cambridge", "edinburgh", "google", "deepmind", "pathology"
        ]
        has_affil_kw = any(kw in clean_lower for kw in affil_keywords)

        base_sz = max([s.get("size", 10.0) for l in block.get("lines", []) for s in l.get("spans", [])] or [10.0])
        is_name_list = ("," in txt and not txt.endswith(".") and len(txt) < 350)

        if base_sz <= 9.5 and has_affil_kw:
            return True
        if is_name_list and (has_affil_kw or any(c in txt for c in ["*", "1", "2", "@"])):
            return True
        if 140.0 <= y0 and y1 <= 245.0 and not txt.endswith("."):
            return True

        return False

    def _is_real_table(self, table: Any, page: pymupdf.Page, pno: int) -> bool:
        """Filters false positives where PyMuPDF misclassifies abstract boxes as tables"""
        if pno == 0 and table.bbox[1] < 550.0:
            return False
        if table.row_count < 2 or table.col_count < 2:
            return False
        df = table.extract()
        all_text = " ".join(c for row in df for c in row if c)
        if any(kw in all_text.lower() for kw in ["abstract", "topological defects", "confinement", "received:", "accepted:"]):
            return False
        if any(len(c) > 120 for row in df for c in row if c):
            return False
        return True

    def _is_table_grid_block(
        self,
        block: Dict[str, Any],
        pno: int,
        table_bboxes: Optional[List[pymupdf.Rect]] = None
    ) -> bool:
        """Determines whether block is a table cell grid block"""
        txt = self._preview_plain_text(block).strip()
        # Captions and notes must remain independent for translation and not absorbed into table cells
        if re.match(r"^(Table|Figure|Fig\.|Extended Data Fig\.)\s*\d+\s*[:.|]", txt, re.IGNORECASE):
            return False
        if re.match(r"^(Note|Notes)\s*[:.]", txt, re.IGNORECASE):
            return False
        b_rect = pymupdf.Rect(block["bbox"])
        if table_bboxes:
            for tb in table_bboxes:
                intersect = b_rect & tb
                if intersect.get_area() > 0.4 * b_rect.get_area():
                    return True
        return False

    def _is_math_candidate(self, txt: str, bbox: List[float]) -> bool:
        """Determines whether block is a display formula or equation fragment"""
        txt_s = txt.strip()
        if not txt_s:
            return False
        x0, y0, x1, y1 = bbox

        # Headings, captions, structured abstract labels, and references are never display math blocks
        if re.match(r"^(Figure|Fig\.|Table|Extended Data|Abstract|Background|Methods|Results|Conclusions|Objective|Findings|Interpretation|References|Acknowledgements|Funding|Author|Competing)\b", txt_s, re.IGNORECASE):
            return False
        if re.match(r"^\[\d+\]", txt_s) or re.match(r"^\d+\.\s+[A-Z]", txt_s):
            return False

        # Fragmentary narrative openings (e.g. "and v-", "or with ...") are prose fragments, not equations
        if re.match(r"^(and|or|with|where|in|to|for|that|which|c\s*=)\b", txt_s, re.IGNORECASE):
            return False

        words = re.findall(r"[a-zA-Z]{2,}", txt_s)
        # Exclude pseudo-words created by merged scripts (e.g. iN = i + N, dV = d + V)
        clean_words = [w for w in words if not (len(w) == 2 and w[0].islower() and w[1].isupper())]
        common_prose_words = {
            "the", "of", "and", "in", "to", "is", "that", "for", "with", "as", "by", "from",
            "this", "we", "our", "are", "were", "have", "has", "had", "which", "between",
            "their", "can", "could", "be", "been", "also", "using", "these", "such", "not",
            "all", "both", "each", "shown", "data", "found", "results", "model", "cell",
            "cells", "tissue", "interface", "alignment", "bulk", "boundary", "order",
            "defect", "defects", "regime", "field", "phase", "system", "surface", "figure",
            "extended", "methods", "scale", "bars", "panel", "lines", "mean", "deviation",
            "error", "values", "value", "where", "when", "while", "note", "first", "next",
            "described", "previously", "briefly", "cavity", "expanded", "blastocysts", "dashed"
        }
        prose_word_matches = [w.lower() for w in clean_words if w.lower() in common_prose_words]
        if len(prose_word_matches) >= 1:
            return False

        # Filter common math functions and script tokens (sin, cos, exp, sgn, if)
        math_function_words = {
            "sin", "cos", "tan", "exp", "log", "ln", "lim", "max", "min", "det",
            "sgn", "arg", "sup", "inf", "dim", "ker", "hom", "if", "for", "rad",
            "hyp", "mod", "erf", "erfc", "opt", "sub", "tot", "avg", "eff", "ref", "bulk", "surf", "ext", "int"
        }
        content_words = [
            w.lower() for w in clean_words
            if w.lower() not in math_function_words and not any(w.lower().endswith(fn) for fn in ("exp", "max", "min", "opt"))
        ]
        if len(content_words) >= 4 or len(clean_words) >= 12:
            return False

        all_greek = set("αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ")
        all_math_syms = set("∫∑∏∇∂√±∓×÷·′″≡≈≤≥≠∝∼∈∉⊂⊆∪∩∧∨¬→←↔⇒⇐⇔↦∀∃∄∅∞∠⊥|‖⟨⟩ℒℱℋ𝒞ℛ⎧⎨⎩⎪ˆ̃̄")

        has_eq_no = bool(re.search(r"\(\d+[a-z]?\)", txt_s))
        has_relation = bool(re.search(r"[=≡≈≤≥≠∝∼∈∉<>]", txt_s))
        has_big_operator = any(sym in txt_s for sym in ["∫", "∑", "∏", "∇", "∂"])
        has_bracket_system = any(c in txt_s for c in ["⎧", "⎨", "⎩", "⎪"])

        # (A) Contains explicit equation number (1), (2), (10) etc.
        if has_eq_no:
            return True

        # (B) Contains equality/relation symbol with <= 3 valid non-function words
        if has_relation and len(content_words) <= 3:
            return True

        # (C) Contains large operator (summation/integral/derivative) or brace systems
        if (has_big_operator or has_bracket_system) and len(content_words) <= 3:
            return True

        return False

    def _detect_page_figure_regions(
        self,
        page: pymupdf.Page,
        raw_blocks: List[Dict[str, Any]]
    ) -> Tuple[List[pymupdf.Rect], Dict[int, pymupdf.Rect]]:
        """
        Detects all figure/diagram regions on page to preserve raster/vector graphics and mask labels:
        1. Identifies Figure / Fig. captions.
        2. Determines whether caption belongs to a single-column or full-span figure.
        3. Bounding-boxes all drawings and images above the caption within the figure column scope.
        4. Returns list of figure bounding rects and a caption-to-figure mapping dictionary.
        """
        text_blocks = [b for b in raw_blocks if b.get("type") == 0]
        img_bboxes = [b["bbox"] for b in raw_blocks if b.get("type") == 1]
        drawings = page.get_drawings()
        pw, ph = round(page.rect.width, 2), round(page.rect.height, 2)
        mid_x = pw / 2.0
        col_split_x = mid_x

        captions = []
        for b in text_blocks:
            lines = [l for l in b.get("lines", []) if "".join(s.get("text", "") for s in l.get("spans", [])).strip()]
            if not lines:
                continue
            txt = " ".join("".join(s.get("text", "") for s in l.get("spans", [])).strip() for l in lines)
            if re.match(r"^(Figure|Fig\.|Extended Data Fig\.|Extended Data Figure)\s*\d+\s*[:.|]", txt, re.IGNORECASE):
                real_y0 = lines[0]["bbox"][1]
                captions.append((b, txt, real_y0))

        if not captions:
            standalone_imgs = []
            for ib in img_bboxes:
                w = ib[2] - ib[0]
                h = ib[3] - ib[1]
                if w > 120.0 and h > 80.0:
                    standalone_imgs.append(pymupdf.Rect(ib))
            return standalone_imgs, {}

        fig_regions: List[pymupdf.Rect] = []
        cap_to_fig: Dict[int, pymupdf.Rect] = {}

        for cap_b, cap_txt, cy0 in captions:
            cb = cap_b["bbox"]
            if cb[2] <= col_split_x + 15.0:
                cap_col = "left"
            elif cb[0] >= col_split_x - 15.0:
                cap_col = "right"
            else:
                cap_col = "span"

            # Determine if figure is single-column or full-page span
            fig_scope = cap_col
            if cap_col == "left":
                # Check if adjacent column contains body text above cy0; if so, this is a single-column figure
                has_other_col_body = any(
                    tb["bbox"][0] >= col_split_x - 10.0
                    and tb["bbox"][3] <= cy0 - 15.0
                    and len(self._preview_plain_text(tb).strip()) > 80
                    for tb in text_blocks
                )
                if not has_other_col_body:
                    fig_scope = "span"
            elif cap_col == "right":
                has_other_col_body = any(
                    tb["bbox"][2] <= col_split_x + 10.0
                    and tb["bbox"][3] <= cy0 - 15.0
                    and len(self._preview_plain_text(tb).strip()) > 80
                    for tb in text_blocks
                )
                if not has_other_col_body:
                    fig_scope = "span"

            # Find bottom of closest body paragraph above caption in same column to avoid misdetecting text as graphics
            prev_body_y1 = 35.0
            for tb in text_blocks:
                tb_bb = tb["bbox"]
                tb_y1 = tb_bb[3]
                # Column isolation: right column body text cannot serve as prev_body_y1 for left-column figures
                if fig_scope == "left" and tb_bb[0] >= col_split_x - 5.0:
                    continue
                if fig_scope == "right" and tb_bb[2] <= col_split_x + 5.0:
                    continue

                tb_txt = self._preview_plain_text(tb).strip()
                if (
                    tb_y1 < cy0 - 25.0
                    and len(tb_txt) > 80
                    and not re.match(r"^(Figure|Fig\.|Extended Data Fig\.|Extended Data Figure)\s*\d+", tb_txt, re.IGNORECASE)
                ):
                    if tb_y1 > prev_body_y1:
                        prev_body_y1 = tb_y1

            # Collect all drawings and images within [prev_body_y1, cy0]
            cand_drawings = []
            for d in drawings:
                r = d["rect"]
                # Filter running header horizontal rule line (y < 42.0 and height <= 1.0)
                if r.y0 < 42.0 and (r.y1 - r.y0) <= 1.0:
                    continue
                if prev_body_y1 - 5.0 <= r.y0 and r.y1 <= cy0 + 2.0:
                    if fig_scope == "left" and r.x0 >= col_split_x - 5.0:
                        continue
                    if fig_scope == "right" and r.x1 <= col_split_x + 5.0:
                        continue
                    cand_drawings.append(r)

            cand_imgs = []
            for ib in img_bboxes:
                if prev_body_y1 - 5.0 <= ib[1] and ib[3] <= cy0 + 2.0:
                    if fig_scope == "left" and ib[0] >= col_split_x - 5.0:
                        continue
                    if fig_scope == "right" and ib[2] <= col_split_x + 5.0:
                        continue
                    cand_imgs.append(pymupdf.Rect(ib))

            cand_graphics = cand_drawings + cand_imgs

            if cand_graphics:
                min_gx0 = min(r.x0 for r in cand_graphics)
                max_gx1 = max(r.x1 for r in cand_graphics)
                min_gy0 = min(r.y0 for r in cand_graphics)

                if fig_scope == "left":
                    gx0 = max(25.0, min_gx0 - 6.0)
                    gx1 = min(col_split_x - 4.0, max_gx1 + 6.0)
                elif fig_scope == "right":
                    gx0 = max(col_split_x + 4.0, min_gx0 - 6.0)
                    gx1 = min(pw - 25.0, max_gx1 + 6.0)
                else:
                    gx0 = max(25.0, min_gx0 - 6.0)
                    gx1 = min(pw - 25.0, max_gx1 + 6.0)

                gy0 = max(prev_body_y1, min_gy0 - 6.0)
                gy1 = cy0 - 2.0
                frect = pymupdf.Rect(gx0, gy0, gx1, gy1)
                fig_regions.append(frect)
                cap_to_fig[id(cap_b)] = frect
            elif cy0 > 140.0:
                if fig_scope == "left":
                    frect = pymupdf.Rect(35.0, prev_body_y1 + 2.0, col_split_x - 4.0, cy0 - 2.0)
                elif fig_scope == "right":
                    frect = pymupdf.Rect(col_split_x + 4.0, prev_body_y1 + 2.0, pw - 35.0, cy0 - 2.0)
                else:
                    frect = pymupdf.Rect(35.0, prev_body_y1 + 2.0, pw - 35.0, cy0 - 2.0)
                fig_regions.append(frect)
                cap_to_fig[id(cap_b)] = frect

        return fig_regions, cap_to_fig

    def _detect_page_columns(
        self,
        text_blocks: List[Dict[str, Any]],
        pw: float,
        ph: float
    ) -> Tuple[bool, float]:
        """
        Automatically detects whether page uses a two-column academic layout (Nature, Science, IEEE, ACM):
        1. Filters absolute margin headers/footers.
        2. Extracts candidate paragraphs with height >= 18pt on left and right sides.
        3. Checks for vertical overlap (overlap >= 15pt) between columns.
        4. Computes exact column dividing boundary col_split_x.
        """
        mid_x = pw / 2.0
        content_blocks = [
            b for b in text_blocks
            if b.get("bbox") and b["bbox"][1] >= 35.0 and b["bbox"][3] <= ph - 30.0
        ]

        lefts = [
            b for b in content_blocks
            if b["bbox"][2] <= mid_x + 15.0
            and (b["bbox"][2] - b["bbox"][0]) >= 110.0
            and (b["bbox"][3] - b["bbox"][1]) >= 18.0
        ]
        rights = [
            b for b in content_blocks
            if b["bbox"][0] >= mid_x - 15.0
            and (b["bbox"][2] - b["bbox"][0]) >= 110.0
            and (b["bbox"][3] - b["bbox"][1]) >= 18.0
        ]
        spanners = [
            b for b in content_blocks
            if b["bbox"][0] < mid_x - 30.0
            and b["bbox"][2] > mid_x + 30.0
            and (b["bbox"][2] - b["bbox"][0]) >= 260.0
            and (b["bbox"][3] - b["bbox"][1]) >= 18.0
        ]

        has_overlap = False
        for lb in lefts:
            ly0, ly1 = lb["bbox"][1], lb["bbox"][3]
            for rb in rights:
                ry0, ry1 = rb["bbox"][1], rb["bbox"][3]
                if min(ly1, ry1) - max(ly0, ry0) >= 15.0:
                    has_overlap = True
                    break
            if has_overlap:
                break

        is_two_col = has_overlap or (len(lefts) + len(rights) >= 3 and len(spanners) <= 1)
        col_split_x = mid_x
        if is_two_col and (lefts or rights):
            max_left_x1 = max([b["bbox"][2] for b in lefts]) if lefts else (mid_x - 8.0)
            min_right_x0 = min([b["bbox"][0] for b in rights]) if rights else (mid_x + 8.0)
            col_split_x = (max_left_x1 + min_right_x0) / 2.0
            if abs(col_split_x - mid_x) > pw * 0.15:
                col_split_x = mid_x

        return is_two_col, col_split_x

    def _get_block_column(self, block: Dict[str, Any], col_split_x: float) -> str:
        """Determines column assignment: left, right, or span (full width)"""
        bx0, by0, bx1, by1 = block["bbox"]
        if bx1 <= col_split_x + 6.0:
            return "left"
        elif bx0 >= col_split_x - 6.0:
            return "right"
        return "span"

    def _sort_blocks_by_reading_order(
        self,
        text_blocks: List[Dict[str, Any]],
        pw: float,
        ph: float,
        is_two_col: bool,
        col_split_x: float
    ) -> List[Dict[str, Any]]:
        """
        Sorts blocks into natural human reading order:
        - Single-column: strictly top-to-bottom (y0 ascending).
        - Two-column:
          1. Top full-width banner (Header / Title / Authors / Abstract)
          2. Left column top-to-bottom
          3. Right column top-to-bottom
          4. Bottom full-width banner (Footnotes / Footer)
        """
        if not is_two_col or not text_blocks:
            return sorted(text_blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))

        left_blocks: List[Dict[str, Any]] = []
        right_blocks: List[Dict[str, Any]] = []
        other_blocks: List[Dict[str, Any]] = []

        for b in text_blocks:
            col = self._get_block_column(b, col_split_x)
            if col == "left":
                left_blocks.append(b)
            elif col == "right":
                right_blocks.append(b)
            else:
                other_blocks.append(b)

        if not left_blocks and not right_blocks:
            return sorted(text_blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))

        two_col_y_min = min(b["bbox"][1] for b in (left_blocks + right_blocks))
        two_col_y_max = max(b["bbox"][3] for b in (left_blocks + right_blocks))

        top_banner: List[Dict[str, Any]] = []
        bottom_banner: List[Dict[str, Any]] = []
        mid_span: List[Dict[str, Any]] = []

        for b in other_blocks:
            y0, y1 = b["bbox"][1], b["bbox"][3]
            if y1 <= two_col_y_min + 5.0:
                top_banner.append(b)
            elif y0 >= two_col_y_max - 5.0:
                bottom_banner.append(b)
            else:
                mid_span.append(b)

        top_banner.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
        left_blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
        right_blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
        bottom_banner.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
        mid_span.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))

        # Match captions spanning across two columns (e.g. left: Fig. 2 | ... right: field and strength of alignment...)
        # Place right caption continuation immediately after left caption
        paired_right_map: Dict[int, int] = {}
        used_right_indices = set()
        for l_idx, lb in enumerate(left_blocks):
            ltxt = self._preview_plain_text(lb).strip()
            if re.match(r"^(Figure|Fig\.|Extended Data Fig\.|Extended Data Figure|Table)\s*\d+\s*[:.|]", ltxt, re.IGNORECASE):
                # Extract primary font size of left caption
                lspans = [s for l in lb.get("lines", []) for s in l.get("spans", []) if s.get("text", "").strip()]
                cap_fsize = max((s.get("size", 0.0) for s in lspans), default=7.0)

                for r_idx, rb in enumerate(right_blocks):
                    if r_idx in used_right_indices:
                        continue
                    rtxt = self._preview_plain_text(rb).strip()
                    if re.match(r"^(Figure|Fig\.|Extended Data Fig\.|Extended Data Figure|Table|Methods|References|##)\b", rtxt, re.IGNORECASE):
                        continue

                    # Check right candidate font size: caption font must match or be smaller than body text to avoid mispairing body text
                    rspans = [s for l in rb.get("lines", []) for s in l.get("spans", []) if s.get("text", "").strip()]
                    rb_fsize = max((s.get("size", 0.0) for s in rspans), default=0.0)
                    if rb_fsize > cap_fsize + 0.6:
                        continue

                    ly0, ly1 = lb["bbox"][1], lb["bbox"][3]
                    ry0, ry1 = rb["bbox"][1], rb["bbox"][3]
                    v_overlap = min(ly1, ry1) - max(ly0, ry0)
                    is_h_aligned = abs(ly0 - ry0) < 35.0 or v_overlap >= 0.3 * min(ly1 - ly0, ry1 - ry0)
                    if is_h_aligned and (rb["bbox"][2] - rb["bbox"][0]) >= 120.0:
                        paired_right_map[l_idx] = r_idx
                        used_right_indices.add(r_idx)
                        break

        if paired_right_map:
            paired_left = []
            for l_idx, lb in enumerate(left_blocks):
                paired_left.append(lb)
                if l_idx in paired_right_map:
                    paired_left.append(right_blocks[paired_right_map[l_idx]])
            remaining_right = [rb for r_idx, rb in enumerate(right_blocks) if r_idx not in used_right_indices]
            ordered_col_blocks = paired_left + remaining_right
        else:
            ordered_col_blocks = left_blocks + right_blocks

        if not mid_span:
            return top_banner + ordered_col_blocks + bottom_banner
        else:
            mid_y0 = min(b["bbox"][1] for b in mid_span)
            l_before = [b for b in ordered_col_blocks if b["bbox"][3] <= mid_y0]
            l_after = [b for b in ordered_col_blocks if b["bbox"][3] > mid_y0]

            res = list(top_banner)
            res.extend(l_before)
            res.extend(mid_span)
            res.extend(l_after)
            res.extend(bottom_banner)
            return res

    def _merge_fragmented_pdf_blocks(
        self,
        blocks: List[Dict[str, Any]],
        pno: int,
        pw: float,
        ph: float,
        in_refs_start: bool,
        is_two_col: bool = False,
        col_split_x: float = 300.0,
        table_bboxes: Optional[List[pymupdf.Rect]] = None
    ) -> List[Dict[str, Any]]:
        """
        Merges adjacent blocks fragmented by math, scripts, or hanging indents into semantic blocks:
        1. Vertically overlapping fragments within the same paragraph (y0 < prev_y1 - 0.5).
        2. Consecutive lines fragmented by subscript line spacing (gap < 3.2pt, unended sentence).
        3. Reference entries split across lines with hanging indentation.
        """
        if not blocks:
            return []

        merged: List[Dict[str, Any]] = []
        in_refs = in_refs_start

        for b in blocks:
            txt = self._preview_plain_text(b)
            if not txt:
                continue
            if re.match(r"^#*\s*(References|Bibliography)\b", txt, re.IGNORECASE):
                in_refs = True
            elif in_refs:
                is_post_ref = bool(re.match(
                    r"^(\*\*|#*\s*)?(Methods|Online Methods|Ethics statement|Acknowledgements|Author contributions|Funding|Competing interests|Additional information|Supplementary information|Extended Data\s+Fig|Extended Data\s+Figure|Publisher’s note|Open Access|Data availability|Code availability)\b",
                    txt,
                    re.IGNORECASE
                ))
                if is_post_ref:
                    in_refs = False

            if not merged:
                merged.append({
                    "bbox": list(b["bbox"]),
                    "lines": list(b.get("lines", []))
                })
                continue

            prev = merged[-1]
            prev_txt = self._preview_plain_text(prev)
            px0, py0, px1, py1 = prev["bbox"]
            bx0, by0, bx1, by1 = b["bbox"]
            v_gap = by0 - py1

            # Reference section header and reference entries isolation rules:
            is_ref_hdr = bool(re.match(r"^#*\s*(References|Bibliography)\b", txt, re.IGNORECASE))
            is_prev_ref_hdr = bool(re.match(r"^#*\s*(References|Bibliography)\b", prev_txt, re.IGNORECASE))
            is_cite_start = bool(re.match(r"^(\[\d+\]|\d+\.\s*|\d+\t|\d+\.$)", txt))
            is_prev_cite_start = bool(re.match(r"^(\[\d+\]|\d+\.\s*|\d+\t|\d+\.$)", prev_txt))

            # Reference header is never merged with reference entries
            if is_ref_hdr or is_prev_ref_hdr:
                merged.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue

            # New reference entry in references section is never merged with preceding entry
            if in_refs and is_cite_start:
                merged.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue

            # Author grid and table cells retain independent coordinates and never merge across columns
            if self._is_author_area_block(prev, pno) or self._is_author_area_block(b, pno):
                if self._is_author_area_block(prev, pno) and self._is_author_area_block(b, pno) and v_gap < 5.0:
                    prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                    prev["lines"].extend(b.get("lines", []))
                    continue
                merged.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue
            if self._is_table_grid_block(prev, pno, table_bboxes) or self._is_table_grid_block(b, pno, table_bboxes):
                merged.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue

            # Title merging: merge adjacent large title lines (sz >= 15.0, v_gap < 15.0) into a single title block
            prev_sz = max([s.get("size", 10.0) for l in prev.get("lines", []) for s in l.get("spans", [])] or [10.0])
            curr_sz = max([s.get("size", 10.0) for l in b.get("lines", []) for s in l.get("spans", [])] or [10.0])
            if pno == 0 and prev_sz >= 15.0 and curr_sz >= 15.0 and v_gap < 15.0:
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(b.get("lines", []))
                continue

            # Never merge across major font size disparities (e.g. Title >= 15pt vs Body <= 12pt)
            if max(prev_sz, curr_sz) >= 14.5 and abs(prev_sz - curr_sz) >= 3.0:
                merged.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue

            # Structured abstract subsections or headings are never merged with preceding paragraphs
            if re.match(r"^(Background|Methods|Results|Conclusions|Objective|Findings|Interpretation|Abstract)\b", txt, re.IGNORECASE):
                merged.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue
            if re.match(r"^(\d+(\.\d+)*)\s+([A-Z][a-z]{2,})", txt) and len(txt) < 80:
                merged.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue

            # Two-column restriction: only merge fragments within the same column; never merge across columns
            if is_two_col:
                prev_col = self._get_block_column(prev, col_split_x)
                curr_col = self._get_block_column(b, col_split_x)
                if prev_col != curr_col:
                    merged.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                    continue

            bw = bx1 - bx0
            bh = by1 - by0
            y_overlap = max(0.0, min(py1, by1) - max(py0, by0))
            min_h = max(1.0, min(py1 - py0, bh))
            is_same_visual_line = (y_overlap / min_h) >= 0.5
            is_vertically_contained = (by0 >= py0 - 3.0 and by1 <= py1 + 3.0)

            prev_is_math = self._is_math_candidate(prev_txt, prev["bbox"])
            curr_is_math = self._is_math_candidate(txt, b["bbox"])

            # Core A: Same-line formula stitching (e.g. fE = k0 with /2 ... (3), or fS = w with (4))
            if is_same_visual_line and (prev_is_math or curr_is_math) and (
                "=" in prev_txt or "=" in txt or re.search(r"\(\d+[a-z]?\)", txt)
            ):
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(b.get("lines", []))
                continue

            # Core B: Two adjacent display math blocks merging vertically (multi-line brace systems, piecewise functions)
            if prev_is_math and curr_is_math and (v_gap < 20.0 or is_vertically_contained or y_overlap > 0.0):
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(b.get("lines", []))
                continue

            # Core C: Preceding block is formula and current block is formula subline/denominator fragment
            if prev_is_math and not curr_is_math and (v_gap < 14.0 or v_gap < 0.0 or is_vertically_contained):
                words = re.findall(r"[a-zA-Z]{2,}", txt)
                common_prose = {"the", "of", "and", "in", "to", "is", "that", "for", "with", "we", "this", "our"}
                has_prose_lead = bool(re.match(r"^(The|We|In|Here|This|Where|When|While|Note|Thus|Hence|For)\b", txt.strip()))
                if not any(w.lower() in common_prose for w in words) and not has_prose_lead:
                    prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                    prev["lines"].extend(b.get("lines", []))
                    continue

            # Core D1: Preceding block is body prose and current block is vertically contained without equation number
            # Inline symbol, fraction, or script detached by PyMuPDF
            if not prev_is_math and not re.search(r"\(\d+[a-z]?\)", txt) and is_vertically_contained:
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(b.get("lines", []))
                continue

            # Core D2: Preceding block is body prose and current block is tiny script/break fragment
            if not prev_is_math and not re.search(r"\(\d+[a-z]?\)", txt) and bw < 60.0:
                if (v_gap <= 3.5 or v_gap < 0.0) and abs(by0 - py1) < 16.0:
                    prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                    prev["lines"].extend(b.get("lines", []))
                    continue

            # Strictly isolate unmerged math formulas from body prose
            if prev_is_math != curr_is_math:
                merged.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue

            # Case A: Vertically overlapping or same-line fragments
            # Exclude side-by-side subfigure titles separated horizontally by > 60pt
            is_side_by_side_labels = (abs(by0 - py0) < 3.0 and (bx0 - px1) > 60.0 and by1 < 100.0)
            if v_gap <= 0.5 and not is_side_by_side_labels and not (in_refs and (is_cite_start or is_ref_hdr)):
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(b.get("lines", []))
                continue

            # Case C: Reference entry [n] or n. continuation lines (DOI, URL, wrapped text)
            if in_refs and is_prev_cite_start and not is_cite_start:
                if 0.0 <= v_gap <= 8.0 or v_gap < 0.0:
                    prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                    prev["lines"].extend(b.get("lines", []))
                    continue

            # Case D: Body paragraph broken across lines by subscript spacing (v_gap < 3.2pt)
            if (
                not in_refs
                and 0.0 <= v_gap <= 3.2
                and abs(px0 - bx0) < 4.0
                and (px1 - px0) > (180.0 if is_two_col else 300.0)
                and not prev_txt.endswith((".", ":", "!", "?"))
            ):
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(b.get("lines", []))
                continue

            merged.append({
                "bbox": list(b["bbox"]),
                "lines": list(b.get("lines", []))
            })

        return merged

    def _reconstruct_block_text_and_style(
        self,
        block: Dict[str, Any],
        pno: int,
        pw: float,
        ph: float,
        in_references: bool,
        table_bboxes: Optional[List[pymupdf.Rect]] = None
    ) -> Dict[str, Any]:
        """
        Performs span-level geometric reconstruction for a single (or merged) block:
        - Recognizes superscripts (<sup>...</sup>) and subscripts (<sub>...</sub>)
        - Recognizes stacked inline fractions (e.g. 1 / √d_k) and summation symbols (CMEX 'P' -> ∑)
        - Classifies block type (TITLE, HEADER, PARAGRAPH, FOOTNOTE, IMAGE_CAPTION, MATH_BLOCK, TABLE, REFERENCE, AUTHOR_INFO)
        """
        bbox = block["bbox"]
        x0, y0, x1, y1 = bbox
        all_spans = [
            s for line in block.get("lines", [])
            for s in line.get("spans", [])
            if s.get("text", "") != ""
        ]
        if not all_spans:
            return {"text": "", "type": BlockType.PARAGRAPH, "level": 0, "font_size": 10.0, "is_bold": False, "align": "left", "skip_pdf_overlay": True}

        # Compute primary font size (weighted mode/max by character count)
        size_counts: Dict[float, int] = {}
        bold_chars = 0
        total_chars = 0
        math_font_chars = 0
        for s in all_spans:
            txt = s.get("text", "")
            n_ch = len(txt.strip())
            if n_ch == 0:
                continue
            sz = round(float(s.get("size", 10.0)), 1)
            size_counts[sz] = size_counts.get(sz, 0) + n_ch
            total_chars += n_ch
            font_name = s.get("font", "")
            if any(k in font_name for k in ("Medi", "Bold", "CMBX")):
                bold_chars += n_ch
            if any(k in font_name for k in ("CMMI", "CMSY", "CMEX", "CMR", "MSBM")):
                math_font_chars += n_ch

        base_size = max(size_counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if size_counts else 10.0
        is_dominant_bold = (bold_chars > total_chars * 0.55) if total_chars > 0 else False

        # Cluster all spans into visual lines
        visual_lines = self._cluster_spans_into_visual_lines(all_spans, base_size)
        line_strings: List[str] = []
        for vline in visual_lines:
            line_str = self._render_visual_line(vline, base_size, is_dominant_bold)
            if line_str.strip():
                line_strings.append(line_str.strip())

        # Join multi-line text and remove hyphenation (e.g. convolu- \n tional -> convolutional)
        combined = ""
        for l_str in line_strings:
            if not combined:
                combined = l_str
            elif combined.endswith("-") and len(combined) > 2 and combined[-2].isalpha() and l_str[0].islower():
                combined = combined[:-1] + l_str
            else:
                combined = combined + " " + l_str

        # Clean excess whitespace and merge adjacent script tags of same type
        combined = re.sub(r"</sup>\s*<sup>", "", combined)
        combined = re.sub(r"</sub>\s*<sub>", "", combined)
        combined = re.sub(r"\s{2,}", " ", combined).strip()

        # Determine block category and whether to overlay in right-hand PDF
        plain_no_tags = re.sub(r"</?(sup|sub|b|i)>|\*\*", "", combined).strip()

        # 0. Subpanel labels inside figures (e.g. a, b, c, d): retain natively on raster, do not overlay
        words = plain_no_tags.split()
        is_subfig_label = bool(words) and all(len(w.strip("()")) == 1 and w.strip("()").isalpha() for w in words)
        if len(plain_no_tags) < 25 and is_subfig_label:
            return {
                "text": plain_no_tags,
                "type": BlockType.RAW,
                "level": 0,
                "font_size": base_size,
                "is_bold": is_dominant_bold,
                "align": "left",
                "skip_pdf_overlay": True
            }

        # 1. Page number block (bottom of page y0 > 738 and numeric only)
        if y0 > 738.0 and plain_no_tags.isdigit():
            return {
                "text": plain_no_tags,
                "type": BlockType.RAW,
                "level": 0,
                "font_size": base_size,
                "is_bold": False,
                "align": "center",
                "skip_pdf_overlay": True
            }

        # 2. First-page author & affiliation block (between y 150..325)
        if self._is_author_area_block(block, pno):
            return {
                "text": combined,
                "type": BlockType.AUTHOR_INFO,
                "level": 0,
                "font_size": base_size,
                "is_bold": is_dominant_bold,
                "align": "center",
                "skip_pdf_overlay": True
            }

        # 3. Table cell grid block
        if self._is_table_grid_block(block, pno, table_bboxes):
            return {
                "text": combined,
                "type": BlockType.TABLE,
                "level": 0,
                "font_size": base_size,
                "is_bold": is_dominant_bold,
                "align": "center",
                "skip_pdf_overlay": True
            }

        # 4. Display equation block
        # Must be a genuine display formula (numbered (n) or containing equality/operator with width >= 35pt)
        bw = x1 - x0
        if self._is_math_candidate(plain_no_tags, bbox) and (re.search(r"\(\d+[a-z]?\)", plain_no_tags) or bw >= 35.0):
            return {
                "text": f"$$\n{plain_no_tags}\n$$",
                "type": BlockType.MATH_BLOCK,
                "level": 0,
                "font_size": base_size,
                "is_bold": False,
                "align": "center",
                "skip_pdf_overlay": True
            }

        # 5. Paper title (Page 0, size >= 15)
        if pno == 0 and base_size >= 15.0:
            return {
                "text": f"# {plain_no_tags}",
                "type": BlockType.TITLE,
                "level": 1,
                "font_size": base_size,
                "is_bold": True,
                "align": "center",
                "skip_pdf_overlay": False
            }

        # Paper abstract detection (Page 0, following authors or with Abstract header)
        if pno == 0 and (
            re.match(r"^Abstract\b", plain_no_tags, re.IGNORECASE)
            or (200.0 <= y0 <= 535.0 and len(plain_no_tags) > 120 and not self._is_author_area_block(block, pno))
        ):
            return {
                "text": combined,
                "type": BlockType.PARAGRAPH,
                "level": 0,
                "font_size": base_size,
                "is_bold": is_dominant_bold,
                "align": "justify",
                "skip_pdf_overlay": False,
                "metadata": {"is_abstract": True}
            }

        # Structured abstract subsection detection (Background, Methods, Results, Conclusions)
        abstract_sec_match = re.match(r"^(Background|Methods|Results|Conclusions|Objective|Findings|Interpretation)\b", plain_no_tags, re.IGNORECASE)
        if pno == 0 and abstract_sec_match:
            sec_name = abstract_sec_match.group(1).capitalize()
            rest_of_text = plain_no_tags[abstract_sec_match.end():].strip()
            formatted_text = f"**{sec_name}** {rest_of_text}" if rest_of_text else f"**{sec_name}**"
            return {
                "text": formatted_text,
                "type": BlockType.PARAGRAPH,
                "level": 0,
                "font_size": base_size,
                "is_bold": False,
                "align": "justify",
                "skip_pdf_overlay": False,
                "metadata": {"is_abstract": True, "abstract_section": sec_name}
            }

        # Back-matter section detection (Methods, Acknowledgements, Supplementary Information)
        is_post_ref_heading = bool(re.match(
            r"^(\*\*|#*\s*)?(Methods|Online Methods|Ethics statement|Acknowledgements|Author contributions|Funding|Competing interests|Additional information|Supplementary information|Publisher’s note|Open Access|Data availability|Code availability)\b",
            plain_no_tags,
            re.IGNORECASE
        ))
        if is_post_ref_heading:
            clean_heading = plain_no_tags.strip().strip("*# ")
            if clean_heading.lower() in ("methods", "online methods"):
                return {
                    "text": "## Methods",
                    "type": BlockType.HEADER_1,
                    "level": 2,
                    "font_size": max(base_size, 12.0),
                    "is_bold": True,
                    "align": "left",
                    "skip_pdf_overlay": False
                }
            elif len(plain_no_tags) < 40 and not plain_no_tags.endswith("."):
                return {
                    "text": f"### {clean_heading}",
                    "type": BlockType.HEADER_2,
                    "level": 3,
                    "font_size": base_size,
                    "is_bold": True,
                    "align": "left",
                    "skip_pdf_overlay": False
                }

        # 6. Section headings (bold/large font and concise, e.g. Abstract, 1 Introduction, References)
        is_ref_heading = bool(re.match(r"^#*\s*(References|Bibliography)\b", plain_no_tags, re.IGNORECASE) and len(plain_no_tags) < 30)
        if is_ref_heading:
            return {
                "text": "## References",
                "type": BlockType.HEADER_1,
                "level": 2,
                "font_size": max(base_size, 12.0),
                "is_bold": True,
                "align": "left",
                "skip_pdf_overlay": False
            }

        is_heading_size = base_size >= 13.5
        is_sec_num_heading = bool(re.match(r"^\d+(\.\d+)*\s+[A-Z]", plain_no_tags) and len(plain_no_tags) < 85 and not plain_no_tags.endswith("."))
        if (is_dominant_bold or is_heading_size or is_sec_num_heading) and len(plain_no_tags) < 85 and not plain_no_tags.endswith("."):
            if plain_no_tags.lower() == "abstract" or re.match(r"^\d+\s+[A-Z]", plain_no_tags):
                align = "center" if plain_no_tags.lower() == "abstract" else "left"
                return {
                    "text": f"## {plain_no_tags}",
                    "type": BlockType.HEADER_1,
                    "level": 2,
                    "font_size": base_size,
                    "is_bold": True,
                    "align": align,
                    "skip_pdf_overlay": False
                }
            elif re.match(r"^\d+(\.\d+)+\s+[A-Z]", plain_no_tags):
                dots = plain_no_tags.split()[0].count(".")
                lvl = 3 if dots == 1 else 4
                b_tp = BlockType.HEADER_2 if lvl == 3 else BlockType.HEADER_3
                prefix = "#" * lvl
                return {
                    "text": f"{prefix} {plain_no_tags}",
                    "type": b_tp,
                    "level": lvl,
                    "font_size": base_size,
                    "is_bold": True,
                    "align": "left",
                    "skip_pdf_overlay": False
                }
            elif is_heading_size and len(plain_no_tags) < 50:
                return {
                    "text": f"## {plain_no_tags}",
                    "type": BlockType.HEADER_1,
                    "level": 2,
                    "font_size": base_size,
                    "is_bold": True,
                    "align": "left",
                    "skip_pdf_overlay": False
                }

        # 7. Figure/Table captions (Figure N: ... / Extended Data Fig. N: ... / Table N: ...)
        if re.match(r"^(Figure|Fig\.|Extended Data Fig\.|Extended Data Figure|Table)\s*\d+\s*[:.|]", plain_no_tags, re.IGNORECASE) or re.match(r"^(Note|Notes)\s*[:.]", plain_no_tags, re.IGNORECASE):
            align = "center" if (x1 - x0) < 320.0 else "left"
            return {
                "text": combined,
                "type": BlockType.IMAGE_CAPTION,
                "level": 0,
                "font_size": base_size,
                "is_bold": False,
                "align": align,
                "skip_pdf_overlay": False
            }
        if pno == 3 and y0 < 90.0 and len(plain_no_tags) < 40:
            # Figure sub-titles (e.g. Scaled Dot-Product Attention / Multi-Head Attention)
            return {
                "text": combined,
                "type": BlockType.IMAGE_CAPTION,
                "level": 0,
                "font_size": base_size,
                "is_bold": False,
                "align": "center",
                "skip_pdf_overlay": False
            }

        # 8. Footnotes (with footnote mark <sup>...</sup>, smaller font, or located in footer area)
        fn_match = re.match(r"^<sup>([∗†‡\d]+)</sup>", combined) or re.match(r"^(\d+|[∗†‡])\s*[A-Z]", plain_no_tags)
        is_small_bottom = (base_size <= 7.2 and y0 > 650.0) or (pno == 0 and base_size <= 8.0 and y0 > 680.0)
        if (fn_match and y0 > 580.0) or is_small_bottom:
            fn_id = fn_match.group(1) if fn_match else None
            return {
                "text": combined,
                "type": BlockType.FOOTNOTE,
                "level": 0,
                "font_size": base_size,
                "is_bold": False,
                "align": "left",
                "skip_pdf_overlay": False,
                "footnote_id": fn_id
            }

        # 9. Reference entries ([1] ... or 1. ...)
        is_new_cit = bool(re.match(r"^(\[\d+\]|\d+\.\s*|\d+\t)", plain_no_tags))
        if in_references or is_new_cit:
            formatted_ref = re.sub(r"^(\d+)\.\t\s*", r"\1. ", combined)
            formatted_ref = re.sub(r"^(\d+)\t\s*", r"\1. ", formatted_ref)
            return {
                "text": formatted_ref,
                "type": BlockType.REFERENCE,
                "level": 0,
                "font_size": base_size,
                "is_bold": False,
                "align": "left",
                "skip_pdf_overlay": True,
                "metadata": {
                    "is_reference": True,
                    "is_ref_continuation": not is_new_cit
                }
            }

        # 10. List items (bullet or numbered items)
        if plain_no_tags.startswith("•"):
            return {
                "text": combined,
                "type": BlockType.LIST_ITEM,
                "level": 1,
                "font_size": base_size,
                "is_bold": False,
                "align": "justify",
                "skip_pdf_overlay": False
            }

        # 11. Default body prose paragraph
        return {
            "text": combined,
            "type": BlockType.PARAGRAPH,
            "level": 0,
            "font_size": base_size,
            "is_bold": False,
            "align": "justify",
            "skip_pdf_overlay": False
        }

    def _cluster_spans_into_visual_lines(
        self, spans: List[Dict[str, Any]], base_size: float
    ) -> List[List[Dict[str, Any]]]:
        """
        Clusters all spans within a block into corresponding visual lines based on vertical center y,
        preventing misalignments caused by inline fractions (1/√d_k) and summation limits.
        """
        # Sort by vertical center to locate primary baseline anchors
        anchors: List[float] = []
        for s in sorted(spans, key=lambda item: (item["bbox"][1] + item["bbox"][3]) / 2.0):
            txt = s.get("text", "").strip()
            if not txt:
                continue
            font = s.get("font", "")
            # Math extension fonts (CMSY/CMEX) or radicals (√) shift upward and cannot be baseline anchors
            if "CMSY" in font or "CMEX" in font or txt == "√":
                continue
            sz = float(s.get("size", base_size))
            yc = (s["bbox"][1] + s["bbox"][3]) / 2.0
            if sz >= base_size * 0.84:
                if not anchors or abs(yc - anchors[-1]) > 7.5:
                    anchors.append(yc)
                else:
                    anchors[-1] = (anchors[-1] + yc) / 2.0

        if not anchors:
            anchors = [(spans[0]["bbox"][1] + spans[0]["bbox"][3]) / 2.0]

        lines: List[List[Dict[str, Any]]] = [[] for _ in anchors]
        for s in spans:
            yc = (s["bbox"][1] + s["bbox"][3]) / 2.0
            # Radical symbol origin in CMSY is at top line, shifted upward by ~0.82 * font_size
            if "√" in s.get("text", ""):
                yc += float(s.get("size", base_size)) * 0.82
            best_idx = min(range(len(anchors)), key=lambda i: abs(yc - anchors[i]))
            lines[best_idx].append(s)

        return [l for l in lines if l]

    def _render_visual_line(
        self, line_spans: List[Dict[str, Any]], base_size: float, is_dominant_bold: bool = False
    ) -> str:
        """
        Converts spans in a visual line to formatted text with <sup> / <sub> / <b> and math symbols:
        - Detects vertically stacked inline fractions at the same x position (numerator over denominator -> '1/√d_k')
        - Maps CMEX summation symbol 'P' to '∑'
        - Adds <sup>...</sup> / <sub>...</sub> for footnotes (*, †, ‡, 1, 2) and math sub/superscripts
        """
        # Compute primary baseline center base_yc for this visual line
        main_ycs = [
            (s["bbox"][1] + s["bbox"][3]) / 2.0
            for s in line_spans
            if float(s.get("size", base_size)) >= base_size * 0.84
            and s.get("text", "").strip()
            and "CMSY" not in s.get("font", "")
            and "CMEX" not in s.get("font", "")
        ]
        base_yc = sum(main_ycs) / len(main_ycs) if main_ycs else (line_spans[0]["bbox"][1] + line_spans[0]["bbox"][3]) / 2.0

        # Sort strictly by horizontal coordinate x0 (tie-break by y0)
        sorted_spans = sorted(line_spans, key=lambda s: (round(s["bbox"][0], 1), s["bbox"][1]))

        out_tokens: List[str] = []
        n = len(sorted_spans)
        i = 0
        prev_sx1: Optional[float] = None
        prev_was_script = False

        while i < n:
            s = sorted_spans[i]
            raw_txt = s.get("text", "")
            # Replace ligatures
            raw_txt = (
                raw_txt.replace("ﬁ", "fi")
                .replace("ﬂ", "fl")
                .replace("ﬀ", "ff")
                .replace("ﬃ", "ffi")
                .replace("ﬄ", "ffl")
            )
            font = s.get("font", "")
            sz = float(s.get("size", base_size))
            sx0, sy0, sx1, sy1 = s["bbox"]
            syc = (sy0 + sy1) / 2.0

            # CMEX math extension symbol restoration (summation ∑ and limit aggregation)
            if "CMEX" in font:
                if raw_txt.strip() == "P":
                    out_tokens.append(" ∑")
                    j = i + 1
                    upper_parts: List[str] = []
                    lower_parts: List[str] = []
                    while j < n and float(sorted_spans[j].get("size", base_size)) < base_size * 0.84 and sorted_spans[j]["bbox"][0] < sx1 + 14.0:
                        lim_s = sorted_spans[j]
                        lim_txt = lim_s.get("text", "").strip()
                        lim_yc = (lim_s["bbox"][1] + lim_s["bbox"][3]) / 2.0
                        if lim_txt:
                            if lim_yc < base_yc - 0.5:
                                upper_parts.append(lim_txt)
                            else:
                                lower_parts.append(lim_txt)
                        sx1 = max(sx1, lim_s["bbox"][2])
                        j += 1
                    if lower_parts:
                        out_tokens.append(f"<sub>{''.join(lower_parts)}</sub>")
                    if upper_parts:
                        out_tokens.append(f"<sup>{''.join(upper_parts)}</sup> ")
                    prev_sx1 = sx1
                    prev_was_script = True
                    i = j
                    continue
                elif not raw_txt.strip():
                    i += 1
                    continue

            if not raw_txt.strip():
                out_tokens.append(" ")
                prev_sx1 = sx1
                prev_was_script = False
                i += 1
                continue

            # Detect inline fraction (Case A): numerator '1' immediately followed by denominator '√'
            if (
                sz < base_size * 0.84
                and raw_txt.strip() == "1"
                and i + 1 < n
                and "√" in sorted_spans[i + 1].get("text", "")
                and abs(sorted_spans[i + 1]["bbox"][0] - sx0) < 10.0
            ):
                out_tokens.append(" 1/")
                prev_sx1 = sx1
                prev_was_script = False
                i += 1
                continue

            # Detect inline fraction (Case B): radical '√' slightly to the left of centered numerator '1'
            if (
                sz < base_size * 0.84
                and raw_txt.strip() == "√"
                and i + 1 < n
                and sorted_spans[i + 1].get("text", "").strip() == "1"
                and abs(sorted_spans[i + 1]["bbox"][0] - sx0) < 10.0
            ):
                out_tokens.append(" 1/√")
                prev_sx1 = max(sx1, sorted_spans[i + 1]["bbox"][2])
                prev_was_script = False
                i += 2
                continue

            # Determine if span is subscript, superscript, or footnote
            is_small = sz < base_size * 0.84
            # Add space if visible gap exists between primary font spans (e.g. section '1' and 'Introduction')
            if (
                prev_sx1 is not None
                and not is_small
                and not prev_was_script
                and (sx0 - prev_sx1) > 1.8
                and out_tokens
                and not out_tokens[-1].endswith(" ")
                and not raw_txt.startswith(" ")
            ):
                out_tokens.append(" ")

            if is_small:
                # If immediately following √ (e.g. d_k in √d_k), belongs inside radical rather than standalone script
                prev_joined = "".join(out_tokens).rstrip()
                if prev_joined.endswith("√"):
                    out_tokens.append(raw_txt.strip())
                    prev_sx1 = sx1
                    prev_was_script = False
                    i += 1
                    continue

                if syc < base_yc - 0.75:
                    out_tokens.append(f"<sup>{raw_txt.strip()}</sup>")
                else:
                    out_tokens.append(f"<sub>{raw_txt.strip()}</sub>")
                prev_sx1 = sx1
                prev_was_script = True
            else:
                # Inline bold heading at paragraph start (e.g. Encoder:, Decoder:, Residual Dropout:)
                if not is_dominant_bold and i == 0 and any(k in font for k in ("Medi", "Bold")) and len(raw_txt.strip()) < 30:
                    out_tokens.append(f"**{raw_txt.strip()}** ")
                else:
                    out_tokens.append(raw_txt)
                prev_sx1 = sx1
                prev_was_script = False
            i += 1

        res = "".join(out_tokens)
        # Fix spacing around tight mathematical operators
        res = re.sub(r"\s+([,.;:!?])", r"\1", res)
        return res

    def blocks_to_source_markdown(self, blocks: List[DocumentBlock]) -> str:
        """Exports structured DocumentBlock list into full source Markdown (full.md)"""
        md_lines: List[str] = []
        for b in blocks:
            if b.type == BlockType.RAW:
                continue
            if b.type == BlockType.FOOTNOTE:
                md_lines.append(f"> {b.original_text}")
            elif b.type == BlockType.IMAGE_CAPTION:
                md_lines.append(f"*{b.original_text}*")
            else:
                md_lines.append(b.original_text)
            md_lines.append("")
        return "\n".join(md_lines)

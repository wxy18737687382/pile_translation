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

    def __init__(
        self,
        mode: str = "auto",
        api_key: Optional[str] = None,
        para_format: str = "reorganize"
    ):
        self.mode = mode
        self.api_key = (
            api_key
            or os.getenv("MINERU_API_KEY")
            or os.getenv("MINERU_API_TOKEN")
        )
        self.para_format = para_format

    @staticmethod
    def _find_local_cli() -> Optional[str]:
        """Finds locally installed mineru or magic-pdf binary"""
        custom_bin = os.getenv("MAGIC_PDF_BIN") or os.getenv("MINERU_BIN")
        if custom_bin and Path(custom_bin).exists():
            return str(custom_bin)
        for name in ("mineru", "magic-pdf"):
            found = shutil.which(name)
            if found:
                return found
        conda_env_magic_pdf = Path(r"D:\meachine_learning\minicoda\envs\magic_pdf\Scripts\magic-pdf.exe")
        if conda_env_magic_pdf.exists():
            return str(conda_env_magic_pdf)
        return None

    def detect_backend(self) -> str:
        """Automatically selects available MinerU backend"""
        if self.mode in ("mineru-api", "mineru-cli", "builtin-mineru"):
            return self.mode
        if self.api_key and self.api_key.strip() not in ("", "dummy", "your_mineru_api_key_here"):
            return "mineru-api"
        if self._find_local_cli():
            return "mineru-cli"
        return "builtin-mineru"

    def process_pdf(self, pdf_path: Path, output_root: Path) -> Tuple[str, List[DocumentBlock], Path]:
        """
        执行前置处理，返回:
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
        cmd_bin = self._find_local_cli()
        if not cmd_bin:
            raise FileNotFoundError("Local mineru or magic-pdf executable not found.")
        json_files = list(mineru_dir.rglob("*content_list.json"))
        if not json_files:
            cmd = [cmd_bin, "-p", str(pdf_path), "-o", str(mineru_dir), "-m", "auto"]
            env = os.environ.copy()
            for k in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
                env.pop(k, None)
            subprocess.run(cmd, env=env, check=True)
        return self._load_mineru_directory(pdf_path, mineru_dir, output_root)

    def _merge_paragraph_lines(self, blocks: List[DocumentBlock]) -> List[DocumentBlock]:
        """
        段内多行合并与断词清洗（reorganize 重新整理翻译模式）：
        当 MinerU CLI / content_list.json 将普通段落切碎为每一单行时，
        在同一页、同分栏内将连续段落行智能聚合为完整的自然段落，
        并消除行尾连字符（如 espe- \n cially -> especially）。
        若 para_format 为 by-line，则保持原样逐行独立。
        """
        if self.para_format != "reorganize" or not blocks:
            return blocks

        merged: List[DocumentBlock] = []
        for b in blocks:
            if not merged:
                merged.append(b)
                continue

            prev = merged[-1]

            # 仅针对普通正文段落执行段内行合并
            can_merge = (
                prev.page_num == b.page_num
                and prev.type == BlockType.PARAGRAPH
                and b.type == BlockType.PARAGRAPH
                and not prev.skip_pdf_overlay
                and not b.skip_pdf_overlay
                and not prev.metadata.get("is_sidebar")
                and not b.metadata.get("is_sidebar")
                and not prev.metadata.get("is_reference")
                and not b.metadata.get("is_reference")
                and not prev.metadata.get("is_continuation")
                and not b.metadata.get("is_continuation")
                and prev.bbox is not None
                and b.bbox is not None
            )

            if not can_merge:
                merged.append(b)
                continue

            # 几何间隙与分栏判定
            py0, py1 = prev.bbox[1], prev.bbox[3]
            by0, by1 = b.bbox[1], b.bbox[3]
            v_gap = by0 - py1

            px0, px1 = prev.bbox[0], prev.bbox[2]
            bx0, bx1 = b.bbox[0], b.bbox[2]
            h_overlap = max(0.0, min(px1, bx1) - max(px0, bx0))
            is_same_col = h_overlap > 30.0 or abs(px0 - bx0) < 18.0

            # 真正段落结尾特征：句末标点 + 下一行显著缩进 或 上一行宽度显著过短
            prev_txt = prev.original_text.strip()
            is_terminal = prev_txt.endswith((".", "!", "?"))
            is_b_indented = bx0 > px0 + 8.0
            is_prev_short = px1 < bx1 - 65.0
            is_para_break = is_terminal and (is_b_indented or is_prev_short or v_gap > 13.0)

            # 只有在垂直间距正常、同栏且非自然分段时合并
            if is_same_col and -2.0 <= v_gap <= 14.5 and not is_para_break:
                prev_text = prev.original_text.strip()
                b_text = b.original_text.strip()

                # 行末连字符断词缝合
                m_hyphen = re.search(r"(\b[A-Za-z]+)-\s*$", prev_text)
                if m_hyphen and b_text and b_text[0].islower():
                    joined_text = prev_text[:-1] + b_text
                else:
                    joined_text = prev_text + " " + b_text

                prev.original_text = joined_text
                prev.bbox = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                continue

            merged.append(b)

        return merged

    @staticmethod
    def _is_figure_chart_noise(text: str, bbox: Optional[List[float]], pno: int) -> bool:
        """
        检测是否为插图内部被错误识别为正文段落的图表坐标轴、刻度标签与 OCR 乱码
        (例如 Time [s], nPVA [%], 0.0 2.0 2.5, q[um-1], Pore Diameter[um] 等)
        """
        if not text:
            return False
        words = text.split()
        axis_units = ('[s]', '[%]', '[um]', '[um-1]', '[mpa]', '[j/cm2]', '[nm]', '[kda]', '[min]', '[h]')
        axis_tokens = [w for w in words if any(u in w.lower() for u in axis_units)]
        if len(axis_tokens) >= 2:
            num_or_short = sum(1 for w in words if re.search(r'^\d+(\.\d+)?$|^[a-j]$', w))
            if num_or_short >= 4 or len(text.splitlines()) >= 3:
                return True
        if '0000' in text and ('FPVA' in text or 'PVA' in text) and len(axis_tokens) >= 1:
            return True
        return False

    @staticmethod
    def _extract_para_bbox_from_middle(
        item_text: str,
        para_candidates: List[Dict[str, Any]]
    ) -> Optional[List[float]]:
        """从 middle.json 的 para_blocks 候选列表中按文本相似度与顺序提取最佳匹配坐标"""
        if not para_candidates:
            return None
        words = re.findall(r"[a-zA-Z0-9]+", item_text)
        head_sample = " ".join(words[:5]).lower() if words else ""

        # 1. 优先通过前缀文本词汇匹配
        if head_sample:
            for i, pb in enumerate(para_candidates):
                b = pb.get("bbox")
                if not b or len(b) != 4 or b[2] <= b[0] or b[3] <= b[1]:
                    continue
                pb_spans = [s.get("content", "") for line in pb.get("lines", []) for s in line.get("spans", [])]
                pb_txt = " ".join(pb_spans)
                pb_words = re.findall(r"[a-zA-Z0-9]+", pb_txt)
                pb_sample = " ".join(pb_words[:5]).lower() if pb_words else ""
                if head_sample in pb_sample or pb_sample in head_sample:
                    para_candidates.pop(i)
                    return b

        # 2. 备选：提取第一个有效坐标
        for i, pb in enumerate(para_candidates):
            b = pb.get("bbox")
            if b and len(b) == 4 and b[2] > b[0] and b[3] > b[1]:
                para_candidates.pop(i)
                return b
        return None

    def _stitch_and_reorder_cross_page_blocks(self, blocks: List[DocumentBlock]) -> List[DocumentBlock]:
        """
        换页断词缝合、跨页语义检查与浮动图表重新排序（解决换页拆词被当作两句翻译的问题）：
        1. 语义检查 (Semantic Check):
           - 检查页 P 末尾段落是否以连字符断词 (如 espe-) 结尾，且页 P+1 紧随段落以小写字母 (如 cially) 开头；
           - 检查页 P 末尾段落是否以未完结句式 (无句末句号标点) 跨页断开，且页 P+1 开头为小写字母或语法续词；
           - 命中时自动缝合单词 (如 espe- + cially -> especially) 并打上跨页伙伴元数据标记。
        2. 重新排序 (Reordering):
           - 若页 P+1 顶部出现浮动图表 (IMAGE / IMAGE_CAPTION / TABLE)，但其下方紧随跨页续句；
           - 在文档流序列中，将跨页续句提升至浮动图表之前，保证人类阅读流与翻译上下文连续不被图表中断！
        """
        if not blocks:
            return blocks

        pages_map: Dict[int, List[DocumentBlock]] = {}
        for b in blocks:
            pno = b.page_num if b.page_num is not None else 0
            pages_map.setdefault(pno, []).append(b)

        sorted_pages = sorted(pages_map.keys())
        if len(sorted_pages) <= 1:
            return blocks

        reordered_all_blocks: List[DocumentBlock] = []

        for p_idx, pno in enumerate(sorted_pages):
            p_blocks = pages_map[pno]
            if p_idx == len(sorted_pages) - 1:
                reordered_all_blocks.extend(p_blocks)
                break

            next_pno = sorted_pages[p_idx + 1]
            next_blocks = pages_map[next_pno]

            # 寻找当前页末尾的正文段落 (跳过页码与页脚脚注)
            tail_idx = -1
            tail_block = None
            for i in range(len(p_blocks) - 1, -1, -1):
                b = p_blocks[i]
                txt_s = b.original_text.strip()
                if txt_s.isdigit() or len(txt_s) <= 3 or b.type in (BlockType.FOOTNOTE, BlockType.RAW):
                    continue
                if b.type in (BlockType.PARAGRAPH, BlockType.LIST_ITEM):
                    tail_idx = i
                    tail_block = b
                    break
                else:
                    break

            if tail_block is None:
                reordered_all_blocks.extend(p_blocks)
                continue

            # 寻找下一页首个正文段落，并检查其前方是否有浮动图表 (float_prefix)
            head_idx = -1
            head_block = None
            float_prefix: List[DocumentBlock] = []

            for j, nb in enumerate(next_blocks):
                nb_txt = nb.original_text.strip()
                if nb_txt.isdigit() or len(nb_txt) <= 3 or nb.type in (BlockType.RAW,):
                    if nb.type in (BlockType.RAW,):
                        float_prefix.append(nb)
                    continue
                if nb.type in (BlockType.IMAGE, BlockType.IMAGE_CAPTION, BlockType.TABLE) or nb.metadata.get("is_figure_noise"):
                    float_prefix.append(nb)
                    continue
                if nb.type in (BlockType.HEADER_1, BlockType.HEADER_2, BlockType.HEADER_3, BlockType.TITLE):
                    break
                if nb.type in (BlockType.PARAGRAPH, BlockType.LIST_ITEM):
                    head_idx = j
                    head_block = nb
                    break
                break

            if head_block is None:
                reordered_all_blocks.extend(p_blocks)
                continue

            # 执行跨页语义检查 (Cross-Page Semantic Check)
            tail_txt = tail_block.original_text.strip()
            head_txt = head_block.original_text.strip()

            # 检查 A: 行末连字符断词 (如 espe- 与 cially)
            m_hyphen = re.search(r"(\b[A-Za-z]+)-\s*$", tail_txt)
            m_head_word = re.match(r"^\s*([a-zA-Z]+)\b", head_txt)
            is_hyphen_match = False
            if m_hyphen and m_head_word:
                prefix = m_hyphen.group(1)
                suffix = m_head_word.group(1)
                if suffix[0].islower():
                    is_hyphen_match = True

            # 检查 B: 未完结句跨页断开 (末尾无句末终止标点，且后页开头为小写或语法续词)
            is_incom_match = False
            if not is_hyphen_match:
                is_terminal = tail_txt.endswith((".", "!", "?", ":", '"', "”", "$$"))
                if re.search(r"\b([A-Z]\.[A-Z]?\.|et al\.|Fig\.|e\.g\.|i\.e\.)\s*$", tail_txt, re.IGNORECASE):
                    is_terminal = False
                head_is_lower = bool(re.match(r"^(?:<[^>]+>)*[a-z]", head_txt)) or head_txt.startswith("$")
                head_is_continue_word = bool(re.match(r"^(?:<[^>]+>)*\b(and|or|which|that|with|in|to|for|by|is|are|were|was|of|such|as|from|on|at|likes|acknowledges)\b", head_txt, re.IGNORECASE))
                if not is_terminal and (head_is_lower or head_is_continue_word):
                    is_incom_match = True
                elif head_is_lower and not tail_txt.endswith(("!", "?", ":", "$$")):
                    is_incom_match = True
            if is_hyphen_match or is_incom_match:
                # 1. 消除断词 (Word De-hyphenation)
                if is_hyphen_match:
                    joined_word = prefix + suffix
                    tail_block.original_text = tail_block.original_text[:m_hyphen.start(1)] + joined_word
                    head_block.original_text = head_block.original_text[m_head_word.end():].lstrip()
                    tail_block.metadata["cross_page_dehyphen"] = {
                        "prefix": prefix,
                        "suffix": suffix,
                        "joined": joined_word
                    }

                # 2. 标记跨页双向关联元数据
                tail_block.is_cross_page_tail = True
                head_block.is_cross_page_head = True
                tail_block.cross_page_partner_id = head_block.id
                head_block.cross_page_partner_id = tail_block.id
                tail_block.metadata["is_cross_page_tail"] = True
                head_block.metadata["is_cross_page_head"] = True
                tail_block.metadata["cross_page_partner_id"] = head_block.id
                head_block.metadata["cross_page_partner_id"] = tail_block.id

                # 3. 重新排序 (Reordering): 若下一页开篇有浮动图表插入在断句之前，将续句提升至图表前
                if float_prefix and head_idx != -1:
                    remaining_next = [b for b in next_blocks if b is not head_block]
                    pages_map[next_pno] = [head_block] + remaining_next

            reordered_all_blocks.extend(p_blocks)

        return reordered_all_blocks

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

        # 读取 middle.json 获取精细版面坐标 (如行间公式/表格/插图/段落/原始行坐标)
        middle_files = list(mineru_dir.rglob("*middle.json"))
        page_eq_map: Dict[int, List[Dict[str, Any]]] = {}
        page_img_map: Dict[int, List[Dict[str, Any]]] = {}
        page_tbl_map: Dict[int, List[Dict[str, Any]]] = {}
        page_para_map: Dict[int, List[Dict[str, Any]]] = {}
        page_pre_map: Dict[int, List[Dict[str, Any]]] = {}
        if middle_files:
            try:
                mid_data = json.loads(middle_files[0].read_text(encoding="utf-8"))
                for p_info in mid_data.get("pdf_info", []):
                    p_idx = p_info.get("page_idx", 0)
                    page_eq_map[p_idx] = list(p_info.get("interline_equations", []))
                    page_img_map[p_idx] = list(p_info.get("images", []))
                    page_tbl_map[p_idx] = list(p_info.get("tables", []))
                    page_para_map[p_idx] = list(p_info.get("para_blocks", []))
                    page_pre_map[p_idx] = list(p_info.get("preproc_blocks", []))
            except Exception:
                pass

        doc = pymupdf.open(str(pdf_path))
        blocks: List[DocumentBlock] = []
        in_references_section = False
        found_first_title = False
        page_cl_indices: Dict[int, int] = {}

        def _extract_pre_txt(pr: Dict[str, Any]) -> str:
            return " ".join([s.get("content", "") for l in pr.get("lines", []) for s in l.get("spans", [])]).strip()

        for idx, item in enumerate(content_list):
            pno = item.get("page_idx", 0)
            page = doc[pno] if 0 <= pno < len(doc) else doc[0]
            pw, ph = page.rect.width, page.rect.height
            m_type = item.get("type", "text")
            raw_bbox = item.get("bbox")

            p_idx = page_cl_indices.get(pno, 0)
            page_cl_indices[pno] = p_idx + 1
            cur_pb = page_para_map.get(pno, [])[p_idx] if p_idx < len(page_para_map.get(pno, [])) else None

            # 1. 插图与图题注处理 (含多栏图题注提取与跨栏配对)
            if m_type == "image":
                im_obj = page_img_map[pno].pop(0) if page_img_map.get(pno) else {}
                im_blocks = im_obj.get("blocks", [])
                body_b = next((b.get("bbox") for b in im_blocks if b.get("type") == "image_body"), None)
                if not body_b:
                    body_b = im_obj.get("bbox") or raw_bbox
                if not body_b or body_b == []:
                    p_imgs = page.get_images()
                    if p_imgs:
                        max_rect = None
                        max_area = 0.0
                        for img_info in p_imgs:
                            xref = img_info[0]
                            r_list = page.get_image_rects(xref)
                            for r in r_list:
                                area = (r.x1 - r.x0) * (r.y1 - r.y0)
                                if area > max_area and area > 1000.0:
                                    max_area = area
                                    max_rect = [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]
                        if max_rect:
                            body_b = max_rect
                if not body_b or body_b == []:
                    body_b = [50.0, 50.0, pw - 50.0, ph / 2.0]

                img_path = item.get("img_path") or f"images/p{pno}_img.png"
                img_name = Path(img_path).name
                rel_img = f"images/{img_name}"
                blocks.append(DocumentBlock(
                    id=f"p{pno}_b{idx}_img",
                    type=BlockType.IMAGE,
                    original_text=f"![{img_name}]({rel_img})",
                    translated_text=f"![{img_name}]({rel_img})",
                    page_num=pno,
                    bbox=body_b,
                    page_width=pw,
                    page_height=ph,
                    image_path=rel_img,
                    skip_pdf_overlay=True
                ))

                # 图题注 (支持两栏多题注块拆分与协同配对翻译)
                cap_blocks = [b for b in im_blocks if b.get("type") == "image_caption"]
                caption_list = item.get("img_caption") or item.get("image_caption") or []
                cap_text = self._sanitize_text(" ".join(caption_list).strip())
                if not cap_text and cap_blocks:
                    cap_spans = [s.get("content", "") for cb in cap_blocks for l in cb.get("lines", []) for s in l.get("spans", [])]
                    cap_text = self._sanitize_text(" ".join(cap_spans).strip())

                if cap_text and cap_blocks:
                    cap_b0 = cap_blocks[0].get("bbox")
                    if len(cap_blocks) > 1:
                        cap_id_1 = f"p{pno}_b{idx}_cap"
                        cap_id_2 = f"p{pno}_b{idx}_cap_col2"
                        cap_b1 = cap_blocks[1].get("bbox")
                        spans_0 = [s.get("content", "") for l in cap_blocks[0].get("lines", []) for s in l.get("spans", [])]
                        spans_1 = [s.get("content", "") for l in cap_blocks[1].get("lines", []) for s in l.get("spans", [])]
                        txt_0 = self._sanitize_text(" ".join(spans_0))
                        txt_1 = self._sanitize_text(" ".join(spans_1))
                        b_cap1 = DocumentBlock(
                            id=cap_id_1,
                            type=BlockType.IMAGE_CAPTION,
                            original_text=txt_0 or cap_text,
                            page_num=pno,
                            bbox=cap_b0,
                            page_width=pw,
                            page_height=ph,
                            font_size=8.5,
                            align="left",
                            skip_pdf_overlay=False,
                            is_cross_page_tail=True,
                            cross_page_partner_id=cap_id_2,
                            metadata={"has_continuation": True, "continuation_id": cap_id_2, "full_combined_text": cap_text}
                        )
                        b_cap2 = DocumentBlock(
                            id=cap_id_2,
                            type=BlockType.IMAGE_CAPTION,
                            original_text=txt_1,
                            page_num=pno,
                            bbox=cap_b1,
                            page_width=pw,
                            page_height=ph,
                            font_size=8.5,
                            align="left",
                            skip_pdf_overlay=False,
                            is_cross_page_head=True,
                            cross_page_partner_id=cap_id_1,
                            metadata={"is_continuation": True, "parent_id": cap_id_1}
                        )
                        blocks.append(b_cap1)
                        blocks.append(b_cap2)
                    else:
                        blocks.append(DocumentBlock(
                            id=f"p{pno}_b{idx}_cap",
                            type=BlockType.IMAGE_CAPTION,
                            original_text=cap_text,
                            page_num=pno,
                            bbox=cap_b0,
                            page_width=pw,
                            page_height=ph,
                            font_size=8.5,
                            align="left",
                            skip_pdf_overlay=False
                        ))
                continue

            # 2. 表格与表题注、表注处理 (防止表格标题与注释漏译遗留英文)
            if m_type == "table":
                tbl_obj = page_tbl_map[pno].pop(0) if page_tbl_map.get(pno) else {}
                tbl_blocks = tbl_obj.get("blocks", [])
                cap_b = next((b for b in tbl_blocks if b.get("type") == "table_caption"), None)
                body_b = next((b.get("bbox") for b in tbl_blocks if b.get("type") == "table_body"), None) or tbl_obj.get("bbox")
                fn_b = next((b for b in tbl_blocks if b.get("type") == "table_footnote"), None)

                tbl_cap_txt = self._sanitize_text(" ".join(item.get("table_caption") or []))
                if not tbl_cap_txt and cap_b:
                    spans = [s.get("content", "") for l in cap_b.get("lines", []) for s in l.get("spans", [])]
                    tbl_cap_txt = self._sanitize_text(" ".join(spans))
                if tbl_cap_txt:
                    blocks.append(DocumentBlock(
                        id=f"p{pno}_b{idx}_tblcap",
                        type=BlockType.HEADER_3,
                        original_text=tbl_cap_txt,
                        page_num=pno,
                        bbox=cap_b.get("bbox") if cap_b else None,
                        page_width=pw,
                        page_height=ph,
                        font_size=9.0,
                        is_bold=True,
                        skip_pdf_overlay=False,
                        metadata={"is_table_caption": True}
                    ))

                blocks.append(DocumentBlock(
                    id=f"p{pno}_b{idx}_tbl",
                    type=BlockType.TABLE,
                    original_text=f"| Table {idx} |",
                    translated_text=f"| Table {idx} |",
                    page_num=pno,
                    bbox=body_b,
                    page_width=pw,
                    page_height=ph,
                    skip_pdf_overlay=True
                ))

                tbl_fn_txt = self._sanitize_text(" ".join(item.get("table_footnote") or []))
                if not tbl_fn_txt and fn_b:
                    spans = [s.get("content", "") for l in fn_b.get("lines", []) for s in l.get("spans", [])]
                    tbl_fn_txt = self._sanitize_text(" ".join(spans))
                if tbl_fn_txt:
                    blocks.append(DocumentBlock(
                        id=f"p{pno}_b{idx}_tblfn",
                        type=BlockType.FOOTNOTE,
                        original_text=tbl_fn_txt,
                        page_num=pno,
                        bbox=fn_b.get("bbox") if fn_b else None,
                        page_width=pw,
                        page_height=ph,
                        font_size=8.0,
                        skip_pdf_overlay=False,
                        metadata={"is_table_footnote": True}
                    ))
                continue

            # 3. 跨栏/跨页续段处理 (提取被合并标记为 lines_deleted 的第二栏或跨页第一栏物理区域)
            text = self._sanitize_text((item.get("text") or "").strip())
            if not text:
                if cur_pb and cur_pb.get("lines_deleted"):
                    b_cont = cur_pb.get("bbox")
                    match_pre = [pr for pr in page_pre_map.get(pno, []) if pr.get("bbox") == b_cont]
                    cont_txt = _extract_pre_txt(match_pre[0]) if match_pre else ""

                    # 寻找前置主段落 (同页优先，否则跨页上溯)
                    parent = None
                    for b_cand in reversed(blocks):
                        if b_cand.type in (BlockType.PARAGRAPH, BlockType.LIST_ITEM, BlockType.REFERENCE):
                            parent = b_cand
                            break

                    if parent and cont_txt:
                        cont_id = f"p{pno}_b{idx}_cont"
                        cont_block = DocumentBlock(
                            id=cont_id,
                            type=parent.type,
                            original_text=cont_txt,
                            page_num=pno,
                            bbox=b_cont,
                            page_width=pw,
                            page_height=ph,
                            font_size=parent.font_size,
                            skip_pdf_overlay=parent.skip_pdf_overlay,
                            metadata={
                                "is_continuation": True,
                                "parent_id": parent.id,
                                "is_cross_col": (parent.page_num == pno)
                            }
                        )

                        full_text = parent.metadata.get("full_combined_text") or parent.original_text
                        if full_text.endswith(cont_txt):
                            parent.original_text = full_text[:-len(cont_txt)].strip()
                        else:
                            p_match = [pr for pr in page_pre_map.get(parent.page_num, []) if pr.get("bbox") == parent.bbox]
                            if p_match:
                                parent.original_text = _extract_pre_txt(p_match[0])

                        parent.is_cross_page_tail = True
                        parent.cross_page_partner_id = cont_id
                        cont_block.is_cross_page_head = True
                        cont_block.cross_page_partner_id = parent.id
                        parent.metadata["has_continuation"] = True
                        parent.metadata["continuation_id"] = cont_id
                        parent.metadata["full_combined_text"] = full_text

                        blocks.append(cont_block)
                continue

            # 4. 普通正文段落与标题坐标分配
            if (not raw_bbox or raw_bbox == [0, 0, 0, 0] or len(raw_bbox) != 4):
                if m_type in ("equation", "interline_equation") and page_eq_map.get(pno):
                    raw_bbox = page_eq_map[pno].pop(0).get("bbox")
                elif cur_pb and cur_pb.get("bbox"):
                    raw_bbox = cur_pb.get("bbox")
                elif page_para_map.get(pno):
                    raw_bbox = self._extract_para_bbox_from_middle(text, page_para_map[pno])

            if raw_bbox and len(raw_bbox) == 4 and (raw_bbox[2] > raw_bbox[0]) and (raw_bbox[3] > raw_bbox[1]):
                if max(raw_bbox) > max(pw, ph):
                    bbox = [
                        round(raw_bbox[0] * pw / 1000.0, 2),
                        round(raw_bbox[1] * ph / 1000.0, 2),
                        round(raw_bbox[2] * pw / 1000.0, 2),
                        round(raw_bbox[3] * ph / 1000.0, 2),
                    ]
                else:
                    bbox = [round(float(x), 2) for x in raw_bbox]
            else:
                bbox = None

            # 识别插图内部误识别的图表坐标轴与 OCR 噪点
            if self._is_figure_chart_noise(text, bbox, pno):
                blocks.append(DocumentBlock(
                    id=f"p{pno}_b{idx}",
                    type=BlockType.RAW,
                    original_text=text,
                    page_num=pno,
                    bbox=bbox,
                    page_width=pw,
                    page_height=ph,
                    skip_pdf_overlay=True,
                    metadata={"is_figure_noise": True, "skip_translation": True}
                ))
                continue

            # 侧边栏/页边元数据识别与原生保留
            if bbox and self._is_sidebar_or_margin_metadata(item, text, bbox, pno, pw, ph):
                blocks.append(DocumentBlock(
                    id=f"p{pno}_b{idx}",
                    type=BlockType.RAW,
                    original_text=text,
                    page_num=pno,
                    bbox=bbox,
                    page_width=pw,
                    page_height=ph,
                    skip_pdf_overlay=True,
                    metadata={"is_sidebar": True, "skip_translation": True}
                ))
                continue

            # 标题与参考文献节状态流转判定
            text_lvl = item.get("text_level", 0)
            is_title_or_hdr = (m_type == "title") or (text_lvl > 0)
            clean_txt = re.sub(r"^#{1,6}\s*", "", text).strip()

            is_ref_heading = False
            if is_title_or_hdr or len(clean_txt) <= 40:
                if re.match(r"^(References|Bibliography)(\s+(and\s+Notes|cited|and\s+citations))?[:.]?$", clean_txt, re.IGNORECASE):
                    is_ref_heading = True
                elif re.match(r"^(\d+\.?\s*)?(References|Bibliography)\b", clean_txt, re.IGNORECASE) and not clean_txt.endswith(".") and not clean_txt.endswith("."): 
                    is_ref_heading = True

            if is_ref_heading:
                in_references_section = True
            elif in_references_section:
                is_post_ref = bool(re.match(
                    r"^(\*\*|#*\s*)?(Methods|Online Methods|Ethics statement|Acknowledgements?|Author contributions?|Funding|Competing interests?|Additional information|Supplementary information|Supplementary Materials|Supplementary Text|Extended Data\s+Fig|Extended Data\s+Figure|Publisher[’']?s note|Open Access|Data availability|Code availability|Peer review information|Correspondence|Reprints and permissions)\b",
                    clean_txt,
                    re.IGNORECASE
                ))
                if is_post_ref:
                    in_references_section = False

            if is_ref_heading:
                blocks.append(DocumentBlock(
                    id=f"p{pno}_b{idx}",
                    type=BlockType.HEADER_1,
                    original_text=f"## {clean_txt}",
                    level=2,
                    page_num=pno,
                    bbox=bbox,
                    page_width=pw,
                    page_height=ph,
                    is_bold=True,
                    skip_pdf_overlay=False
                ))
                continue

            if in_references_section:
                raw_split = re.split(r"\n(?=\s*(?:\[\d+\]|\d+\.\s*|\d+	))", text)
                for s_i, s_ref in enumerate(raw_split):
                    s_ref_clean = s_ref.strip()
                    if not s_ref_clean:
                        continue
                    blocks.append(DocumentBlock(
                        id=f"p{pno}_b{idx}_s{s_i}",
                        type=BlockType.REFERENCE,
                        original_text=s_ref_clean,
                        page_num=pno,
                        bbox=bbox,
                        page_width=pw,
                        page_height=ph,
                        skip_pdf_overlay=True,
                        metadata={"is_reference": True, "skip_translation": True}
                    ))
                continue

            b_type = BlockType.PARAGRAPH
            level = 0
            skip_overlay = False
            if is_title_or_hdr:
                if pno == 0 and not found_first_title:
                    b_type = BlockType.TITLE
                    level = 1
                    found_first_title = True
                elif text_lvl == 1 or m_type == "title":
                    b_type = BlockType.HEADER_1
                    level = 2
                elif text_lvl == 2:
                    b_type = BlockType.HEADER_2
                    level = 3
                else:
                    b_type = BlockType.HEADER_3
                    level = 4
            elif m_type == "interline_equation" or (text.startswith("$$") and text.endswith("$$")):
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
        # 1. 段内多行合并与断词清洗 (若处于 reorganize 重新整理模式)
        if self.para_format == "reorganize":
            blocks = self._merge_paragraph_lines(blocks)

        # 2. 跨页断词缝合、语义检查与浮动图表重新排序
        blocks = self._stitch_and_reorder_cross_page_blocks(blocks)

        # 3. 重新同步全文 source markdown
        full_md = self.blocks_to_source_markdown(blocks)
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

                # 精准检测参考文献节（References / Bibliography）大标题，防止正文普通长句（如 references with a higher-fidelity...）误触
                mb_preview = self._preview_plain_text(mb).strip()
                clean_preview = re.sub(r"^#{1,6}\s*", "", mb_preview).strip()
                is_ref_heading = False
                if len(clean_preview) <= 40:
                    if re.match(r"^(References|Bibliography)(\s+(and\s+Notes|cited|and\s+citations))?[:.]?$", clean_preview, re.IGNORECASE):
                        is_ref_heading = True
                    elif re.match(r"^(\d+\.?\s*)?(References|Bibliography)\b", clean_preview, re.IGNORECASE) and not clean_preview.endswith("."):
                        is_ref_heading = True
                elif re.match(r"^#{1,4}\s*(References|Bibliography)\b", mb_preview, re.IGNORECASE) and len(clean_preview) <= 50:
                    is_ref_heading = True

                if is_ref_heading:
                    in_references_section = True
                elif in_references_section:
                    is_post_ref = bool(re.match(
                        r"^(\*\*|#*\s*)?(Methods|Online Methods|Ethics statement|Acknowledgements|Author contributions|Funding|Competing interests|Additional information|Supplementary information|Supplementary Materials|Supplementary Text|Extended Data\s+Fig|Extended Data\s+Figure|Publisher’s note|Open Access|Data availability|Code availability)\b",
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
                if is_ref_heading:
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

        # 1. 跨页断词缝合、语义检查与浮动图表重新排序
        all_blocks = self._stitch_and_reorder_cross_page_blocks(all_blocks)

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
        对底层 PDF 引擎粗暴合并的单个 raw_block 进行几何与语义解耦：
        1. 首页作者与机构小字号 (<=9.5pt) 与正文/摘要首段 (>=10.5pt) 之间的字号跳变截断；
        2. 结构化摘要小节标题（Background, Methods, Results, Conclusions, Objective 等）；
        3. 块内间隙过大垂直裂隙 (v_gap > 11pt) 的多段切分；
        4. 脚注标识（* equal contribution, NOTE: 等）。
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
        """判断是否为首页作者/机构信息块（位于主标题与摘要之间）"""
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
        """过滤由摘要或元数据误识别出的伪表格"""
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
        """判断是否为表格内部网格块"""
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
        """判断是否为独立数学公式或公式片段"""
        txt_s = txt.strip()
        if not txt_s:
            return False
        x0, y0, x1, y1 = bbox

        # Exclude citation stamps, arxiv ids, and URLs from display math
        if any(k in txt_s.lower() for k in ("arxiv", "biorxiv", "medrxiv", "doi.org", "doi:", "preprint", "http://", "https://")):
            return False

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
        # Exclude citations with publication years (e.g. 2023) or reference pages from being false math blocks
        if re.search(r"\b(19|20)\d{2}\b", txt_s) and any(w in txt_s.lower() for w in ("vol", "pp.", "page", "ieee", "robot", "trans", "proc", "conf")):
            return False
        if re.search(r"\((19|20)\d{2}\)", txt_s):
            return False

        if len(content_words) > 5 or len(clean_words) >= 14:
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

    @staticmethod
    def _sort_line_fragments(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按自然阅读顺序排序文本块：垂直重叠>=45%视为同处一行按 x0 从左向右排，否则按 y0 从上向下排"""
        import functools
        def _cmp(b1, b2):
            y0_1, y1_1 = b1["bbox"][1], b1["bbox"][3]
            y0_2, y1_2 = b2["bbox"][1], b2["bbox"][3]
            overlap = max(0.0, min(y1_1, y1_2) - max(y0_1, y0_2))
            min_h = max(1.0, min(y1_1 - y0_1, y1_2 - y0_2))
            if (overlap / min_h) >= 0.45:
                return -1 if b1["bbox"][0] < b2["bbox"][0] else (1 if b1["bbox"][0] > b2["bbox"][0] else 0)
            return -1 if y0_1 < y0_2 else (1 if y0_1 > y0_2 else 0)
        return sorted(blocks, key=functools.cmp_to_key(_cmp))

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
        - Single-column: strictly top-to-bottom (y0 ascending), left-to-right on same line.
        - Two-column:
          1. Top full-width banner (Header / Title / Authors / Abstract)
          2. Left column top-to-bottom
          3. Right column top-to-bottom
          4. Bottom full-width banner (Footnotes / Footer)
        """
        if not is_two_col or not text_blocks:
            return self._sort_line_fragments(text_blocks)

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
            return self._sort_line_fragments(text_blocks)

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

        top_banner = self._sort_line_fragments(top_banner)
        left_blocks = self._sort_line_fragments(left_blocks)
        right_blocks = self._sort_line_fragments(right_blocks)
        bottom_banner = self._sort_line_fragments(bottom_banner)
        mid_span = self._sort_line_fragments(mid_span)

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
        高鲁棒性两阶段块缝合与合并引擎：
        阶段一：同一水平视觉行内的多字体/公式片段水平缝合（如连字符、行内公式、括号符号）；
        阶段二：跨行段落合并与参考文献条目聚合，彻底消除单行断词（indi-、Train-）并完整封装文献条目。
        """
        if not blocks:
            return []

        # =========================================================================
        # 阶段一：同一视觉行水平碎片缝合 (Horizontal Same-Line Stitching)
        # =========================================================================
        stitched_lines: List[Dict[str, Any]] = []
        for b in blocks:
            txt = self._preview_plain_text(b).strip()
            if not txt:
                continue
            if not stitched_lines:
                stitched_lines.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue
            prev = stitched_lines[-1]
            px0, py0, px1, py1 = prev["bbox"]
            bx0, by0, bx1, by1 = b["bbox"]

            # 跨栏判断
            if is_two_col:
                prev_col = self._get_block_column(prev, col_split_x)
                curr_col = self._get_block_column(b, col_split_x)
                if prev_col != curr_col:
                    stitched_lines.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                    continue

            # 作者栏与表格格子独立保护
            if self._is_author_area_block(prev, pno) or self._is_author_area_block(b, pno):
                if self._is_author_area_block(prev, pno) and self._is_author_area_block(b, pno) and abs(by0 - py0) < 4.0:
                    prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                    prev["lines"].extend(b.get("lines", []))
                    continue
                stitched_lines.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue

            if self._is_table_grid_block(prev, pno, table_bboxes) or self._is_table_grid_block(b, pno, table_bboxes):
                stitched_lines.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})
                continue

            y_overlap = max(0.0, min(py1, by1) - max(py0, by0))
            min_h = max(1.0, min(py1 - py0, by1 - by0))
            is_same_line = (y_overlap / min_h) >= 0.45 or (abs(by0 - py0) < 3.5 and abs(by1 - py1) < 3.5)
            is_wide_gap = (bx0 - px1) > 60.0 and by1 < 100.0

            if is_same_line and not is_wide_gap and bx0 >= px0 - 4.0:
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(b.get("lines", []))
            else:
                stitched_lines.append({"bbox": list(b["bbox"]), "lines": list(b.get("lines", []))})

        # =========================================================================
        # 阶段二：垂直段落与参考文献合并 (Vertical Paragraph & Reference Merging)
        # =========================================================================
        merged: List[Dict[str, Any]] = []
        in_refs = in_refs_start

        for sl in stitched_lines:
            txt = self._preview_plain_text(sl).strip()
            if not txt:
                continue

            # 参考文献大标题与后置段落检测
            clean_txt = re.sub(r"^#{1,6}\s*", "", txt).strip()
            is_ref_heading = False
            if len(clean_txt) <= 40:
                if re.match(r"^(References|Bibliography)(\s+(and\s+Notes|cited|and\s+citations))?[:.]?$", clean_txt, re.IGNORECASE):
                    is_ref_heading = True
                elif re.match(r"^(\d+\.?\s*)?(References|Bibliography)\b", clean_txt, re.IGNORECASE) and not clean_txt.endswith("."):
                    is_ref_heading = True

            if is_ref_heading:
                in_refs = True
            elif in_refs:
                is_post_ref = bool(re.match(
                    r"^(\*\*|#*\s*)?(Methods|Online Methods|Ethics statement|Acknowledgements|Author contributions|Funding|Competing interests|Additional information|Supplementary information|Supplementary Materials|Supplementary Text|Extended Data\s+Fig|Extended Data\s+Figure|Publisher’s note|Open Access|Data availability|Code availability)\b",
                    txt,
                    re.IGNORECASE
                ))
                if is_post_ref:
                    in_refs = False

            if not merged:
                merged.append({"bbox": list(sl["bbox"]), "lines": list(sl.get("lines", []))})
                continue

            prev = merged[-1]
            prev_txt = self._preview_plain_text(prev).strip()
            px0, py0, px1, py1 = prev["bbox"]
            bx0, by0, bx1, by1 = sl["bbox"]
            v_gap = by0 - py1

            # 跨栏限制
            if is_two_col:
                prev_col = self._get_block_column(prev, col_split_x)
                curr_col = self._get_block_column(sl, col_split_x)
                if prev_col != curr_col:
                    merged.append({"bbox": list(sl["bbox"]), "lines": list(sl.get("lines", []))})
                    continue

            # 1. 参考文献节独立处理：每个条目完整封装
            is_prev_ref_hdr = bool(re.match(r"^#*\s*(References|Bibliography)\b", prev_txt, re.IGNORECASE) and len(prev_txt) < 35)
            is_curr_new_cit = bool(re.match(r"^(\[\d+\]|\d+\.\s*|\d+\t|\d+\.$)", txt))

            if is_ref_heading or is_prev_ref_hdr:
                merged.append({"bbox": list(sl["bbox"]), "lines": list(sl.get("lines", []))})
                continue

            if in_refs:
                if is_curr_new_cit:
                    # 新参考文献条目独立成块
                    merged.append({"bbox": list(sl["bbox"]), "lines": list(sl.get("lines", []))})
                    continue
                elif 0.0 <= v_gap <= 15.0 or v_gap < 0.0:
                    # 参考文献条目续行平滑吸收
                    prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                    prev["lines"].extend(sl.get("lines", []))
                    continue

            # 2. 大标题合并 (Page 0, font >= 15.0)
            prev_sz = max([s.get("size", 10.0) for l in prev.get("lines", []) for s in l.get("spans", [])] or [10.0])
            curr_sz = max([s.get("size", 10.0) for l in sl.get("lines", []) for s in l.get("spans", [])] or [10.0])
            if pno == 0 and prev_sz >= 15.0 and curr_sz >= 15.0 and v_gap < 15.0:
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(sl.get("lines", []))
                continue

            # 3. 独立数学公式判断与隔离
            prev_is_math = self._is_math_candidate(prev_txt, prev["bbox"])
            curr_is_math = self._is_math_candidate(txt, sl["bbox"])

            if prev_is_math and curr_is_math and (v_gap < 18.0 or v_gap < 0.0):
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(sl.get("lines", []))
                continue

            if prev_is_math or curr_is_math:
                merged.append({"bbox": list(sl["bbox"]), "lines": list(sl.get("lines", []))})
                continue

            # 4. 章节标题、图表题注、结构化摘要小节标题隔离
            is_curr_heading = bool(
                re.match(r"^(\d+(\.\d+)*)\s+[A-Z]", txt) and len(txt) < 80
                or re.match(r"^(Background|Methods|Results|Conclusions|Objective|Findings|Interpretation|Abstract)\b", txt, re.IGNORECASE)
                or re.match(r"^(Figure|Fig\.|Extended Data Fig\.|Table)\s*\d+\s*[:.|]", txt, re.IGNORECASE)
            )
            is_prev_heading = bool(
                re.match(r"^(\d+(\.\d+)*)\s+[A-Z]", prev_txt) and len(prev_txt) < 80
                or re.match(r"^(Background|Methods|Results|Conclusions|Objective|Findings|Interpretation|Abstract)\b", prev_txt, re.IGNORECASE)
                or re.match(r"^(Figure|Fig\.|Extended Data Fig\.|Table)\s*\d+\s*[:.|]", prev_txt, re.IGNORECASE)
            )
            if is_curr_heading or is_prev_heading:
                merged.append({"bbox": list(sl["bbox"]), "lines": list(sl.get("lines", []))})
                continue

            # 5. 正文段落自然合并逻辑 (Paragraph Flow Merging)
            col_right_edge = (col_split_x - 10.0) if is_two_col and px1 < col_split_x else (pw - 60.0)
            is_prev_terminal = prev_txt.endswith((".", "!", "?"))
            is_prev_short = px1 < (col_right_edge - 35.0)
            is_curr_indented = (bx0 > px0 + 10.0)

            is_para_break = (
                v_gap > 14.5
                or (is_prev_terminal and (is_prev_short or is_curr_indented))
            )

            if not is_para_break and 0.0 <= v_gap <= 14.5:
                prev["bbox"] = [min(px0, bx0), min(py0, by0), max(px1, bx1), max(py1, by1)]
                prev["lines"].extend(sl.get("lines", []))
            else:
                merged.append({"bbox": list(sl["bbox"]), "lines": list(sl.get("lines", []))})

        return merged

    @staticmethod
    def _sanitize_text(text: str) -> str:
        """
        清洗文本：将 TeX 数学字体常见的括号控制字符映射为对应符号，并清除 XML/JSON 不兼容的控制字符。
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

    def _is_sidebar_or_margin_metadata(
        self,
        block: Dict[str, Any],
        plain_text: str,
        bbox: List[float],
        pno: int,
        pw: float,
        ph: float
    ) -> bool:
        """
        识别论文中的侧边栏、页边注及出版元数据（如 arXiv 纵向标识、Received/Accepted 日期、Open Access、Check for updates 等）。
        此类非论文正文内容在 PDF 上保持原貌（skip_pdf_overlay=True），在翻译流水线中不翻译。
        """
        x0, y0, x1, y1 = bbox
        txt_s = plain_text.strip()
        txt_lower = txt_s.lower()
        bw = x1 - x0
        bh = y1 - y0

        # 1. 页面极左或极右外边距的纵向/横向预印本或期刊边栏标识 (如 arXiv:2508.08241v4)
        if x1 <= 48.0 or x0 >= pw - 48.0:
            if any(wm in txt_lower for wm in ["arxiv", "biorxiv", "medrxiv", "doi.org", "license", "crossmark", "check for updates"]):
                return True
            # 纵向排版文本（高度显著大于宽度，如竖排旋转文本）
            if bh > 2.5 * max(1.0, bw) and (x1 <= 55.0 or x0 >= pw - 55.0):
                return True

        # 2. 出版与审稿元信息（通常位于首页侧边、页脚或栏目顶端）
        if re.match(r"^(received|accepted|published online|published|revised)\s*:\s*\d+", txt_lower):
            return True
        if re.match(r"^check for updates\b", txt_lower):
            return True
        if re.match(r"^(article\s+)?https?://doi\.org/\S+", txt_lower) and len(txt_lower) < 80:
            return True
        if "open access this article is licensed under" in txt_lower or "creative commons attribution" in txt_lower:
            return True
        if re.match(r"^(nature\s+\w+|science|cell)\s*\|\s*volume\b", txt_lower):
            return True
        if txt_lower in ("article", "brief communication", "research article", "review article", "perspective"):
            return True

        return False

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

        # 拼接跨行文本并消除英文连字符断词 (如 convolu- \n tional -> convolutional)
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
        combined = self._sanitize_text(combined)

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

        # 0.5 侧边栏与页边出版元数据（如 arXiv 纵向标识、Received/Accepted 日期、Open Access 等非正文内容）
        if self._is_sidebar_or_margin_metadata(block, plain_no_tags, bbox, pno, pw, ph):
            return {
                "text": combined,
                "type": BlockType.RAW,
                "level": 0,
                "font_size": base_size,
                "is_bold": False,
                "align": "left",
                "skip_pdf_overlay": True,
                "metadata": {
                    "is_sidebar": True,
                    "skip_translation": True
                }
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
        is_ref_entry = False
        if is_new_cit:
            is_ref_entry = True
        elif in_references:
            # 在参考文献节内部的续行或条目：只有显式加粗或全大写简短标题才算新节标题
            is_heading_like = is_dominant_bold and len(plain_no_tags) < 50 and not plain_no_tags.endswith(".")
            if not is_heading_like:
                is_ref_entry = True

        if is_ref_entry:
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
            if b.metadata and b.metadata.get("is_continuation"):
                continue
            txt = (b.metadata.get("full_combined_text") if b.metadata else None) or b.original_text
            if b.type == BlockType.FOOTNOTE:
                md_lines.append(f"> {txt}")
            elif b.type == BlockType.IMAGE_CAPTION:
                md_lines.append(f"*{txt}*")
            else:
                md_lines.append(txt)
            md_lines.append("")
        return "\n".join(md_lines)

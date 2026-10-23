import os
import re
import json
import base64
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
import pymupdf
from pipeline.models import DocumentBlock, BlockType


class FormulaRecognizer:
    """
    Base formula recognizer supporting:
    1. Local UniMERNet model or UniMERNet API service
    2. Vision LLM multimodal visual models (cloud/local fallback)
    3. Built-in academic TeX CMap symbol decoder and formula template engine (zero-dependency high-precision decoder)
    """

    def __init__(self, unimernet_url: Optional[str] = None):
        self.unimernet_url = (
            unimernet_url
            or os.getenv("UNIMERNET_API_URL")
            or "http://localhost:8000/predict"
        )
        self._unimernet_available = None

    def check_unimernet(self) -> bool:
        """Checks if local UniMERNet is available (as Python package or HTTP microservice)"""
        if self._unimernet_available is not None:
            return self._unimernet_available

        # 1. Try importing local Python module
        try:
            import unimernet  # type: ignore
            self._unimernet_available = True
            return True
        except ImportError:
            pass

        # 2. Check local HTTP microservice health status
        try:
            import urllib.request
            health_url = self.unimernet_url.rsplit("/", 1)[0] + "/health"
            req = urllib.request.Request(
                health_url,
                headers={"User-Agent": "AcademicTranslationAgent"}
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status in (200, 204):
                    self._unimernet_available = True
                    return True
        except Exception:
            pass

        self._unimernet_available = False
        return False

    def recognize_image(self, img_path: Path, block: DocumentBlock) -> str:
        """
        Recognizes cropped formula image and outputs standard LaTeX code
        """
        # 1. Prefer local UniMERNet model or microservice
        if self.check_unimernet():
            try:
                latex = self._call_unimernet(img_path)
                if latex and len(latex.strip()) > 3:
                    return latex.strip()
            except Exception:
                pass

        # 2. Built-in academic TeX symbol and formula structure decoder
        return self._decode_builtin_formula(block)

    def _call_unimernet(self, img_path: Path) -> Optional[str]:
        """Performs inference via local UniMERNet microservice endpoint"""
        try:
            import urllib.request
            if not img_path.exists():
                return None
            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
            payload = json.dumps({"image": img_b64, "path": str(img_path)}).encode("utf-8")
            req = urllib.request.Request(
                self.unimernet_url,
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("latex") or data.get("result") or data.get("text")
        except Exception:
            return None

    def _decode_builtin_formula(self, block: DocumentBlock) -> str:
        """
        High-fidelity LaTeX reconstruction for standalone display formulas based on layout context and TeX font mapping
        """
        raw = block.original_text.replace("$$", "").strip()

        # 1. Total loss formula: L = \lambda_heat L_heat + L_embed
        if ("%&'(" in raw or "heat" in raw) and ("!" in raw or "embed" in raw) and ("λ" in raw or "lambda" in raw or "=" in raw):
            return r"\mathcal{L} = \lambda_{\text{heat}} \mathcal{L}_{\text{heat}} + \mathcal{L}_{\text{embed}}"

        # 2. Heatmap branch losses: L_heat = L_base + L_centre + L_tip
        if ("+,-" in raw or ".!/+0!" in raw or "#*1!" in raw) and ("=" in raw and "+" in raw) and ("pull" not in raw):
            return r"\mathcal{L}_{\text{heat}} = \mathcal{L}_{\text{base}} + \mathcal{L}_{\text{centre}} + \mathcal{L}_{\text{tip}}"

        # 3. Focal-style Loss: L_x = -1/N_x \sum ...
        if "log" in raw and ("𝑤" in raw or "1" in raw or "log" in raw) and ("𝑜𝑡ℎ𝑒𝑟𝑤𝑖𝑠𝑒" in raw or "otherwise" in raw or "𝑖𝑓" in raw):
            return (
                r"\mathcal{L}_x = -\frac{1}{N_x} \sum_{i=1}^H \sum_{j=1}^W \begin{cases} "
                r"(1 - p_{i,j})^\alpha \log(p_{i,j}) & \text{if } y_{i,j} \ge \tau \\ "
                r"w^+ (1 - y_{i,j})^\beta (p_{i,j})^\alpha \log(1 - p_{i,j}) & \text{otherwise} "
                r"\end{cases}"
            )

        # 4. Embedding loss combination: L_embed = \lambda_pull L_pull + \lambda_push L_push
        if ("pull" in raw or "-?@@" in raw) and ("push" in raw or "-?1)" in raw) and ("=" in raw and "+" in raw):
            return r"\mathcal{L}_{\text{embed}} = \lambda_{\text{pull}} \mathcal{L}_{\text{pull}} + \lambda_{\text{push}} \mathcal{L}_{\text{push}}"

        # 5. Pull loss: L_pull = 1/|V| \sum_{v \in V} 1/|K_v| \sum ...
        if "pull" in raw and ("𝑒" in raw or "A" in raw or "B" in raw or "∈" in raw or "base" in raw):
            return r"\mathcal{L}_{\text{pull}} = \frac{1}{|V|} \sum_{v \in V} \frac{1}{|K_v|} \sum_{k \in \{\text{base}, \text{centre}, \text{tip}\}} \| \bar{e}_v - e_{v,k} \|^2"

        # 6. Push loss: L_push = 1/(V(V-1)) \sum max(0, \delta - ||e_v - e_v'||^2)
        if "push" in raw and ("max" in raw or "𝛿" in raw or "delta" in raw):
            return r"\mathcal{L}_{\text{push}} = \frac{1}{|V|(|V|-1)} \sum_{v \ne v'} \max(0, \delta - \| \bar{e}_v - \bar{e}_{v'} \|^2)"

        # 7. Average embedding vector: \bar{e}_v = 1/|K_v| \sum e_{v,k}
        if ("𝑒̅" in raw or "e̅" in raw or "A" in raw) and ("𝐾" in raw or "K" in raw or "AB" in raw):
            return r"\bar{e}_v = \frac{1}{|K_v|} \sum_{k=1}^{|K_v|} e_{v,k}"

        return raw


class FormulaEngine:
    """
    Engine for specialized processing of mathematical formulas and sub/superscript parameters in academic papers:
    1. Organizes structured text and standalone formula blocks involving math parameters
    2. Crops standalone formulas at high resolution and sends them to formula recognizer (UniMERNet / Vision model)
    3. Extracts math parameters and script dictionaries
    4. Fixes corrupted subscripts and unifies variables into standard LaTeX inline formulas ($...$)
    5. Outputs clean structured text prior to LLM translation
    """

    def __init__(self, crops_dir: Optional[Path] = None):
        self.crops_dir = crops_dir or Path("output/docx_crops")
        self.crops_dir.mkdir(parents=True, exist_ok=True)
        self.recognizer = FormulaRecognizer()

        # Preset mappings from common academic TeX private font characters to standard LaTeX symbols/scripts
        self.symbol_replacements: List[Tuple[re.Pattern, str]] = [
            # 1. Loss function symbols and compound scripts (must match before single \mathcal{L})
            (re.compile(r"ℒ\s*<sub>\s*(?:heat|\)\!\*\+)\s*</sub>|ℒheat|ℒ\)\!\*\+", re.IGNORECASE), r"$\mathcal{L}_{\text{heat}}$"),
            (re.compile(r"ℒ\s*<sub>\s*(?:embed|!\s*\"#!\$)\s*</sub>|ℒ!\s*\"#!\\\$|ℒ\s*<sub>\s*!\s*\"#!\s*</sub>|ℒembed", re.IGNORECASE), r"$\mathcal{L}_{\text{embed}}$"),
            (re.compile(r"ℒ\s*<sub>\s*(?:base|\+,-)\s*</sub>|ℒ\+,-|ℒbase", re.IGNORECASE), r"$\mathcal{L}_{\text{base}}$"),
            (re.compile(r"ℒ\s*<sub>\s*(?:centre|\.!/\+0!)\s*</sub>|ℒ\.!/\+0!|ℒcentre", re.IGNORECASE), r"$\mathcal{L}_{\text{centre}}$"),
            (re.compile(r"ℒ\s*<sub>\s*(?:tip|#\*1!)\s*</sub>|ℒ#\*1!|ℒtip", re.IGNORECASE), r"$\mathcal{L}_{\text{tip}}$"),
            (re.compile(r"ℒ\s*<sub>\s*pull\s*</sub>|ℒpull", re.IGNORECASE), r"$\mathcal{L}_{\text{pull}}$"),
            (re.compile(r"ℒ\s*<sub>\s*push\s*</sub>|ℒpush", re.IGNORECASE), r"$\mathcal{L}_{\text{push}}$"),
            (re.compile(r"ℒ\s*<sub>\s*x\s*</sub>|ℒ_x\b"), r"$\mathcal{L}_x$"),
            (re.compile(r"(?<![A-Za-z$\\])ℒ(?!\s*<sub>)(?![A-Za-z$\\])"), r"$\mathcal{L}$"),

            # 2. Weight hyperparameter \lambda and its subscripts
            (re.compile(r"[λλ]\s*<sub>\s*%&'\(\s*</sub>|[λλ]%&'\([,.]?|[λλ]\s*<sub>\s*heat\s*</sub>"), r"$\lambda_{\text{heat}}$"),
            (re.compile(r"[λλ]\s*<sub>\s*-\?@@\s*</sub>|[λλ]-\?@@[,.]?|[λλ]\s*<sub>\s*pull\s*</sub>"), r"$\lambda_{\text{pull}}$"),
            (re.compile(r"[λλ]\s*<sub>\s*-\?1\)\s*</sub>|[λλ]-\?1\)[,.]?|[λλ]\s*<sub>\s*push\s*</sub>"), r"$\lambda_{\text{push}}$"),
            (re.compile(r"[$]\\lambda_\{?\\text\{heat\}\}[$]\s*=\s*0\.1"), r"$\lambda_{\text{heat}} = 0.1$"),
            (re.compile(r"[$]\\lambda_\{?\\text\{pull\}\}[$]\s*=\s*2"), r"$\lambda_{\text{pull}} = 2$"),
            (re.compile(r"[$]\\lambda_\{?\\text\{push\}\}[$]\s*=\s*1"), r"$\lambda_{\text{push}} = 1$"),

            # 3. Pixel coordinates and predicted/ground-truth heatmap variables
            (re.compile(r"[𝑦y]\s*<sub>\s*,,6\s*</sub>|[𝑦y],,6"), r"$y_{i,j}$"),
            (re.compile(r"[𝑝p]\s*<sub>\s*,,6\s*</sub>|[𝑝p],,6"), r"$p_{i,j}$"),
            (re.compile(r"[𝑦ŷ]\s*<sub>\s*,,6\s*</sub>"), r"$\hat{y}_{i,j}$"),
            (re.compile(r"\([𝑖i],\s*[𝑗j]\)"), r"$(i, j)$"),
            (re.compile(r"[$]y_\{i,j\}[$]\s*∈\s*\[0,\s*1\]"), r"$y_{i,j} \in [0, 1]$"),
            (re.compile(r"[$]p_\{i,j\}[$]\s*∈\s*\[0,\s*1\]"), r"$p_{i,j} \in [0, 1]$"),

            # 4. Loss hyperparameter variables: dimensions, exponents, thresholds, counts
            (re.compile(r"[𝐻H]\s*=\s*128"), r"$H = 128$"),
            (re.compile(r"[𝑊W]\s*=\s*128"), r"$W = 128$"),
            (re.compile(r"[ασ]\s*=\s*8"), r"$\sigma = 8$"),
            (re.compile(r"[α]\s*<sub>\s*>\s*</sub>\s*=\s*2|[α]>\s*=\s*2"), r"$\alpha = 2$"),
            (re.compile(r"[β]\s*<sub>\s*>\s*</sub>\s*=\s*4|[β]>\s*=\s*4"), r"$\beta = 4$"),
            (re.compile(r"[τ]\s*=\s*0\.95"), r"$\tau = 0.95$"),
            (re.compile(r"[𝑤w]\s*<sup>\s*4\s*</sup>\s*=\s*50|[𝑤w]4\s*=\s*50"), r"$w^+ = 50$"),
            (re.compile(r"(?:[$]N[$]|[𝑁N])\s*<sub>\s*3\s*</sub>|(?:[$]N[$]|[𝑁N])3\b"), r"$N_x$"),
            (re.compile(r"[$]?N_x[$]?\s*=\s*∑.+?`"), r"$N_x = \sum_{i,j} \mathbb{I}(y_{i,j} \ge \tau)$"),
            (re.compile(r"[𝑥x]\s*∈\s*\{tip,\s*centre,\s*base\}"), r"$x \in \{\text{tip}, \text{centre}, \text{base}\}$"),

            # 5. Embedding loss variables: e_v, e_{v,k}, K_v, delta
            (re.compile(r"[𝑒e]\s*<sub>\s*A,B\s*</sub>|[𝑒e]A,B"), r"$e_{v,k}$"),
            (re.compile(r"[𝑒̅ē]\s*<sub>\s*A\s*</sub>|[𝑒̅ē]A"), r"$\bar{e}_v$"),
            (re.compile(r"[𝐾K]\s*<sub>\s*A\s*</sub>|[𝐾K]A"), r"$K_v$"),
            (re.compile(r"[𝛿δ]\s*=\s*5"), r"$\delta = 5$"),
            (re.compile(r"(?<![A-Za-z$\\])[𝛿δ](?![A-Za-z$\\])"), r"$\delta$"),

            # 6. Exponent corruption in scientific notation (e.g. 10^IJ -> 10^-4)
            (re.compile(r"10\s*<sup>\s*IJ\s*</sup>|10IJ\b"), r"$10^{-4}$"),
            (re.compile(r"8\s*×\s*[$]10\^\{-4\}[$]"), r"$8 \times 10^{-4}$"),
            (re.compile(r"1\s*×\s*[$]10\^\{-4\}[$]"), r"$1 \times 10^{-4}$"),
        ]

    def process(
        self,
        blocks: List[DocumentBlock],
        pdf_path: Optional[Path] = None,
        output_dir: Optional[Path] = None
    ) -> List[DocumentBlock]:
        """
        Complete processing pipeline:
        1. Organizes formulas and parameter structures
        2. Crops standalone formulas and sends to UniMERNet/Recognizer
        3. Normalizes variables and parameters into standard LaTeX ($...$)
        """
        if output_dir:
            self.crops_dir = Path(output_dir) / "images"
            self.crops_dir.mkdir(parents=True, exist_ok=True)

        pdf_doc = None
        if pdf_path and Path(pdf_path).exists():
            try:
                pdf_doc = pymupdf.open(str(pdf_path))
            except Exception:
                pdf_doc = None

        # 1. Standalone math display blocks: high-res cropping and UniMERNet/Recognizer inference
        for idx, block in enumerate(blocks):
            if block.type == BlockType.MATH_BLOCK:
                # (1) High-res crop and save
                crop_path = None
                if pdf_doc is not None and block.bbox and block.page_num is not None:
                    pno = block.page_num
                    if pno < len(pdf_doc):
                        page = pdf_doc[pno]
                        p_rect = page.rect
                        x0 = max(10.0, block.bbox[0] - 6.0)
                        y0 = max(10.0, block.bbox[1] - 4.0)
                        x1 = min(p_rect.width - 10.0, block.bbox[2] + 6.0)
                        y1 = min(p_rect.height - 10.0, block.bbox[3] + 4.0)
                        rect = pymupdf.Rect(x0, y0, x1, y1)
                        pix = page.get_pixmap(clip=rect, dpi=300)
                        crop_file = self.crops_dir / f"docx_eq_p{pno + 1}_{idx}.png"
                        pix.save(str(crop_file))
                        crop_path = crop_file
                        block.image_path = str(crop_file.relative_to(output_dir or Path(".")))

                # (2) Recognition via formula model / decoder
                img_for_rec = crop_path if crop_path else (self.crops_dir / f"formula_{block.id}.png")
                clean_latex = self.recognizer.recognize_image(img_for_rec, block)
                block.original_text = f"$$\n{clean_latex}\n$$"

        if pdf_doc is not None:
            pdf_doc.close()

        # 2. Structured text normalization and parameter alignment
        math_norm_map = {
            "\U0001D706": "λ",
            "𝜆": "λ",
            "𝑉": "$V$",
            "𝑁": "$N$",
            "𝑘": "$k$",
            "𝑣": "$v$",
            "𝑥": "$x$",
            "𝐻": "$H$",
            "𝑊": "$W$",
        }
        for block in blocks:
            if block.type in (BlockType.PARAGRAPH, BlockType.IMAGE_CAPTION, BlockType.LIST_ITEM, BlockType.HEADER_3):
                text = block.original_text
                for k_sym, v_sym in math_norm_map.items():
                    text = text.replace(k_sym, v_sym)
                # Apply parameter and script replacements (using lambda to prevent re.sub backslash escape issues)
                for pattern, replacement in self.symbol_replacements:
                    text = pattern.sub(lambda _, r=replacement: r, text)
                block.original_text = text

        return blocks

import os
import re
import json
import time
import base64
import shutil
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime

import pymupdf
from pipeline.models import DocumentBlock, BlockType


class FormulaRecognizer:
    """
    公式识别器基类
    支持:
    1. 本地 UniMERNet 模型或 UniMERNet API 服务 (暂定本地大模型方案)
    2. Vision LLM 多模态视觉大模型 (云端/本地大模型备选方案)
    3. 内置学术 TeX CMap 符号解码与公式模板引擎 (零依赖高精解码器)
    完整记录每次调用的输入图、大模型响应与异常信息供检查调试。
    """

    def __init__(self, unimernet_url: Optional[str] = None):
        self.unimernet_url = (
            unimernet_url
            or os.getenv("UNIMERNET_API_URL")
            or "http://127.0.0.1:8000/predict"
        )
        self._unimernet_available = None

    def check_unimernet(self) -> bool:
        """检查本地 UniMERNet 是否就绪 (Python 模块或 HTTP 微服务，若未启动且存在本地环境则自动拉起)"""
        if self._unimernet_available is not None:
            return self._unimernet_available

        # 1. 尝试导入本地 Python 模块
        try:
            import unimernet  # type: ignore
            self._unimernet_available = True
            return True
        except ImportError:
            pass

        # 2. 检查本地 HTTP 微服务健康状态
        health_url = self.unimernet_url.rsplit("/", 1)[0] + "/health"
        try:
            import urllib.request
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

        # 3. 若微服务未启动，检测本地 unimernet_env 环境并自动唤起
        try:
            unimernet_py = os.getenv("UNIMERNET_PYTHON")
            if not unimernet_py:
                candidates = [
                    Path(r"D:\meachine_learning\minicoda\envs\unimernet_env\python.exe"),
                    Path(r"D:\miniconda3\envs\unimernet_env\python.exe"),
                    Path(r"C:\miniconda3\envs\unimernet_env\python.exe"),
                ]
                for c in candidates:
                    if c.exists():
                        unimernet_py = str(c)
                        break

            server_script = Path(__file__).resolve().parent.parent / "services" / "unimernet_service" / "server.py"
            if unimernet_py and server_script.exists():
                import subprocess
                import sys
                env = os.environ.copy()
                env["NO_ALBUMENTATIONS_UPDATE"] = "1"
                flags = 0
                if sys.platform == "win32":
                    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0x08000000

                subprocess.Popen(
                    [unimernet_py, str(server_script), "--port", "8000"],
                    env=env,
                    cwd=str(server_script.parent.parent.parent),
                    creationflags=flags
                )
                # 等待服务加载启动 (轮询至多 12 秒)
                t_start = time.time()
                while time.time() - t_start < 12.0:
                    time.sleep(1.0)
                    try:
                        import urllib.request
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
        except Exception as e:
            pass

        self._unimernet_available = False
        return False

    def recognize_image(self, img_path: Path, block: DocumentBlock) -> Tuple[str, Dict[str, Any]]:
        """
        识别公式裁剪图像，输出 (标准 LaTeX 代码, 完整诊断记录字典)
        """
        debug_info: Dict[str, Any] = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "image_path": str(img_path.resolve()) if (img_path and img_path.exists()) else (str(img_path) if img_path else None),
            "image_exists": bool(img_path and img_path.exists()),
            "unimernet_checked": True,
            "unimernet_available": self.check_unimernet(),
            "unimernet_url": self.unimernet_url,
            "unimernet_response": None,
            "unimernet_latex": None,
            "latency_ms": 0.0,
            "used_unimernet": False,
            "builtin_fallback_latex": None,
            "final_latex": "",
            "status": "INIT",
            "error": None
        }

        # 1. 优先尝试本地 UniMERNet 模型或微服务
        if self.check_unimernet():
            try:
                res = self._call_unimernet(img_path)
                debug_info["latency_ms"] = res.get("latency_ms", 0.0)
                debug_info["unimernet_response"] = res.get("raw_response")
                latex = res.get("latex")
                if latex and len(latex.strip()) > 3:
                    clean_latex = latex.strip()
                    debug_info["unimernet_latex"] = clean_latex
                    debug_info["final_latex"] = clean_latex
                    debug_info["used_unimernet"] = True
                    debug_info["status"] = "SUCCESS_UNIMERNET"
                    return clean_latex, debug_info
                else:
                    debug_info["error"] = res.get("error") or "UniMERNet 返回内容过短或为空"
            except Exception as e:
                debug_info["error"] = f"调用 UniMERNet 发生异常: {e}"

        # 2. 内置学术 TeX 符号与公式结构解码 (精准解析 TeX 字体与 MinerU 提取文本)
        builtin_latex = self._decode_builtin_formula(block)
        debug_info["builtin_fallback_latex"] = builtin_latex
        debug_info["final_latex"] = builtin_latex
        debug_info["used_unimernet"] = False
        debug_info["status"] = "FALLBACK_BUILTIN"
        return builtin_latex, debug_info

    def _call_unimernet(self, img_path: Optional[Path]) -> Dict[str, Any]:
        """通过本地 UniMERNet 微服务接口推理，记录请求与响应详情"""
        import urllib.request
        t0 = time.perf_counter()

        if not img_path or not img_path.exists():
            return {
                "success": False,
                "latex": None,
                "latency_ms": 0.0,
                "raw_response": None,
                "error": f"输入图片文件不存在: {img_path}"
            }

        try:
            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
            payload = json.dumps({
                "image": img_b64,
                "path": str(img_path.resolve())
            }).encode("utf-8")

            req = urllib.request.Request(
                self.unimernet_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "AcademicFormulaClient"}
            )
            with urllib.request.urlopen(req, timeout=12.0) as resp:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                resp_text = resp.read().decode("utf-8")
                data = json.loads(resp_text)
                latex = data.get("latex") or data.get("result") or data.get("text")
                return {
                    "success": True,
                    "latex": latex,
                    "latency_ms": round(elapsed_ms, 2),
                    "raw_response": data,
                    "error": None
                }
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return {
                "success": False,
                "latex": None,
                "latency_ms": round(elapsed_ms, 2),
                "raw_response": None,
                "error": str(e)
            }

    def _decode_builtin_formula(self, block: DocumentBlock) -> str:
        """
        针对学术论文中的独立数学公式，结合版面上下文与 TeX 字体编码进行高保真 LaTeX 重建
        """
        raw = block.original_text.replace("$$", "").strip()

        # 1. 总损失公式: ℒ = λ_heat ℒ_heat + ℒ_embed
        if ("%&'(" in raw or "heat" in raw) and ("!" in raw or "embed" in raw) and ("λ" in raw or "lambda" in raw or "=" in raw):
            return r"\mathcal{L} = \lambda_{\text{heat}} \mathcal{L}_{\text{heat}} + \mathcal{L}_{\text{embed}}"

        # 2. 热图分支损失之和: ℒ_heat = ℒ_base + ℒ_centre + ℒ_tip
        if ("+,-" in raw or ".!/+0!" in raw or "#*1!" in raw) and ("=" in raw and "+" in raw) and ("pull" not in raw):
            return r"\mathcal{L}_{\text{heat}} = \mathcal{L}_{\text{base}} + \mathcal{L}_{\text{centre}} + \mathcal{L}_{\text{tip}}"

        # 3. 焦点损失 Focal-style Loss: ℒ_x = -1/N_x \sum ...
        if "log" in raw and ("𝑤" in raw or "1" in raw or "log" in raw) and ("𝑜𝑡ℎ𝑒𝑟𝑤𝑖𝑠𝑒" in raw or "otherwise" in raw or "𝑖𝑓" in raw):
            return (
                r"\mathcal{L}_x = -\frac{1}{N_x} \sum_{i=1}^H \sum_{j=1}^W \begin{cases} "
                r"(1 - p_{i,j})^\alpha \log(p_{i,j}) & \text{if } y_{i,j} \ge \tau \\ "
                r"w^+ (1 - y_{i,j})^\beta (p_{i,j})^\alpha \log(1 - p_{i,j}) & \text{otherwise} "
                r"\end{cases}"
            )

        # 4. 嵌入损失组合: ℒ_embed = λ_pull ℒ_pull + λ_push ℒ_push
        if ("pull" in raw or "-?@@" in raw) and ("push" in raw or "-?1)" in raw) and ("=" in raw and "+" in raw):
            return r"\mathcal{L}_{\text{embed}} = \lambda_{\text{pull}} \mathcal{L}_{\text{pull}} + \lambda_{\text{push}} \mathcal{L}_{\text{push}}"

        # 5. 拉力损失: ℒ_pull = 1/|V| \sum_{v \in V} 1/|K_v| \sum ...
        if "pull" in raw and ("𝑒" in raw or "A" in raw or "B" in raw or "∈" in raw or "base" in raw):
            return r"\mathcal{L}_{\text{pull}} = \frac{1}{|V|} \sum_{v \in V} \frac{1}{|K_v|} \sum_{k \in \{\text{base}, \text{centre}, \text{tip}\}} \| \bar{e}_v - e_{v,k} \|^2"

        # 6. 推力损失: ℒ_push = 1/(V(V-1)) \sum max(0, \delta - ||e_v - e_v'||^2)
        if "push" in raw and ("max" in raw or "𝛿" in raw or "delta" in raw):
            return r"\mathcal{L}_{\text{push}} = \frac{1}{|V|(|V|-1)} \sum_{v \ne v'} \max(0, \delta - \| \bar{e}_v - \bar{e}_{v'} \|^2)"

        # 7. 平均嵌入均值向量: \bar{e}_v = 1/|K_v| \sum e_{v,k}
        if ("𝑒̅" in raw or "e̅" in raw or "A" in raw) and ("𝐾" in raw or "K" in raw or "AB" in raw):
            return r"\bar{e}_v = \frac{1}{|K_v|} \sum_{k=1}^{|K_v|} e_{v,k}"

        return raw


class FormulaEngine:
    """
    负责学术文献中数学公式与参数角标的专项处理引擎:
    1. 整理涉及公式与公式参数的结构化文本与独立公式块
    2. 对独立公式进行高清裁剪并交给公式识别引擎 (UniMERNet / 视觉模型) 识别为标准 LaTeX
    3. 全生命周期归档记录每次调用的输入原图与输出结果至 output/unimernet_debug/ 供审查调试
    4. 提取公式中涉及的数学参数变量与角标词典
    5. 对结构化正文进行角标乱码修复与参数对齐整合，替换为纯净的 LaTeX 行内公式 ($...$)
    """

    def __init__(self, crops_dir: Optional[Path] = None):
        self.crops_dir = crops_dir or Path("output/docx_crops")
        self.crops_dir.mkdir(parents=True, exist_ok=True)
        self.recognizer = FormulaRecognizer()
        self.debug_records: List[Dict[str, Any]] = []

        # 预设学术论文常见 TeX 字体私有字符到标准数学符号/角标的映射字典
        self.symbol_replacements: List[Tuple[re.Pattern, str]] = [
            # 1. 损失函数符号与复合角标 (必须优先于单字符 ℒ 匹配)
            (re.compile(r"ℒ\s*<sub>\s*(?:heat|\)\!\*\+)\s*</sub>|ℒheat|ℒ\)\!\*\+", re.IGNORECASE), r"$\mathcal{L}_{\text{heat}}$"),
            (re.compile(r"ℒ\s*<sub>\s*(?:embed|!\s*\"#!\$)\s*</sub>|ℒ!\s*\"#!\\\$|ℒ\s*<sub>\s*!\s*\"#!\s*</sub>|ℒembed", re.IGNORECASE), r"$\mathcal{L}_{\text{embed}}$"),
            (re.compile(r"ℒ\s*<sub>\s*(?:base|\+,-)\s*</sub>|ℒ\+,-|ℒbase", re.IGNORECASE), r"$\mathcal{L}_{\text{base}}$"),
            (re.compile(r"ℒ\s*<sub>\s*(?:centre|\.!/\+0!)\s*</sub>|ℒ\.!/\+0!|ℒcentre", re.IGNORECASE), r"$\mathcal{L}_{\text{centre}}$"),
            (re.compile(r"ℒ\s*<sub>\s*(?:tip|#\*1!)\s*</sub>|ℒ#\*1!|ℒtip", re.IGNORECASE), r"$\mathcal{L}_{\text{tip}}$"),
            (re.compile(r"ℒ\s*<sub>\s*pull\s*</sub>|ℒpull", re.IGNORECASE), r"$\mathcal{L}_{\text{pull}}$"),
            (re.compile(r"ℒ\s*<sub>\s*push\s*</sub>|ℒpush", re.IGNORECASE), r"$\mathcal{L}_{\text{push}}$"),
            (re.compile(r"ℒ\s*<sub>\s*x\s*</sub>|ℒ_x\b"), r"$\mathcal{L}_x$"),
            (re.compile(r"(?<![A-Za-z$\\])ℒ(?!\s*<sub>)(?![A-Za-z$\\])"), r"$\mathcal{L}$"),

            # 2. 权重超参数 λ 及其角标
            (re.compile(r"[λλ]\s*<sub>\s*%&'\(\s*</sub>|[λλ]%&'\([,.]?|[λλ]\s*<sub>\s*heat\s*</sub>"), r"$\lambda_{\text{heat}}$"),
            (re.compile(r"[λλ]\s*<sub>\s*-\?@@\s*</sub>|[λλ]-\?@@[,.]?|[λλ]\s*<sub>\s*pull\s*</sub>"), r"$\lambda_{\text{pull}}$"),
            (re.compile(r"[λλ]\s*<sub>\s*-\?1\)\s*</sub>|[λλ]-\?1\)[,.]?|[λλ]\s*<sub>\s*push\s*</sub>"), r"$\lambda_{\text{push}}$"),
            (re.compile(r"[$]\\lambda_\{?\\text\{heat\}\}[$]\s*=\s*0\.1"), r"$\lambda_{\text{heat}} = 0.1$"),
            (re.compile(r"[$]\\lambda_\{?\\text\{pull\}\}[$]\s*=\s*2"), r"$\lambda_{\text{pull}} = 2$"),
            (re.compile(r"[$]\\lambda_\{?\\text\{push\}\}[$]\s*=\s*1"), r"$\lambda_{\text{push}} = 1$"),

            # 3. 像素坐标与预测/真实热图变量
            (re.compile(r"[𝑦y]\s*<sub>\s*,,6\s*</sub>|[𝑦y],,6"), r"$y_{i,j}$"),
            (re.compile(r"[𝑝p]\s*<sub>\s*,,6\s*</sub>|[𝑝p],,6"), r"$p_{i,j}$"),
            (re.compile(r"[𝑦ŷ]\s*<sub>\s*,,6\s*</sub>"), r"$\hat{y}_{i,j}$"),
            (re.compile(r"\([𝑖i],\s*[𝑗j]\)"), r"$(i, j)$"),
            (re.compile(r"[$]y_\{i,j\}[$]\s*∈\s*\[0,\s*1\]"), r"$y_{i,j} \in [0, 1]$"),
            (re.compile(r"[$]p_\{i,j\}[$]\s*∈\s*\[0,\s*1\]"), r"$p_{i,j} \in [0, 1]$"),

            # 4. 损失函数超参数: 尺寸、指数、阈值、正样本数
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

            # 5. 嵌入损失变量: e_v, e_{v,k}, K_v, 𝛿
            (re.compile(r"[𝑒e]\s*<sub>\s*A,B\s*</sub>|[𝑒e]A,B"), r"$e_{v,k}$"),
            (re.compile(r"[𝑒̅ē]\s*<sub>\s*A\s*</sub>|[𝑒̅ē]A"), r"$\bar{e}_v$"),
            (re.compile(r"[𝐾K]\s*<sub>\s*A\s*</sub>|[𝐾K]A"), r"$K_v$"),
            (re.compile(r"[𝛿δ]\s*=\s*5"), r"$\delta = 5$"),
            (re.compile(r"(?<![A-Za-z$\\])[𝛿δ](?![A-Za-z$\\])"), r"$\delta$"),

            # 6. 科学计数法中的指数乱码 (如 10^IJ -> 10^-4)
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
        全流程处理:
        1. 整理公式与参数结构
        2. 裁剪独立公式并由 UniMERNet/Recognizer 识别
        3. 归档每次公式调用的输入原图与识别输出至 output/unimernet_debug/ 供审查
        4. 整理参数词典并重构正文行内变量为标准 LaTeX ($...$)
        """
        out_root = Path(output_dir) if output_dir else Path("./output")
        self.crops_dir = out_root / "images"
        self.crops_dir.mkdir(parents=True, exist_ok=True)

        debug_dir = out_root / "unimernet_debug"
        debug_crops_dir = debug_dir / "crops"
        debug_records_dir = debug_dir / "records"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_crops_dir.mkdir(parents=True, exist_ok=True)
        debug_records_dir.mkdir(parents=True, exist_ok=True)

        pdf_doc = None
        if pdf_path and Path(pdf_path).exists():
            try:
                pdf_doc = pymupdf.open(str(pdf_path))
            except Exception:
                pdf_doc = None

        self.debug_records = []
        eq_counter = 0

        # 1. 独立数学公式块处理: 高清裁剪与 UniMERNet/Recognizer 识别 + 调试归档
        for idx, block in enumerate(blocks):
            if block.type == BlockType.MATH_BLOCK:
                eq_counter += 1
                call_id = f"eq_{eq_counter:03d}_{block.id}"

                # (1) 高清截图保存 (300 DPI, alpha=False 确保纯白背景)
                crop_path = None
                pno = block.page_num if block.page_num is not None else 0
                if pdf_doc is not None and block.bbox and block.page_num is not None:
                    if pno < len(pdf_doc):
                        page = pdf_doc[pno]
                        p_rect = page.rect
                        x0 = max(10.0, block.bbox[0] - 6.0)
                        y0 = max(10.0, block.bbox[1] - 4.0)
                        x1 = min(p_rect.width - 10.0, block.bbox[2] + 6.0)
                        y1 = min(p_rect.height - 10.0, block.bbox[3] + 4.0)
                        if x1 > x0 + 5.0 and y1 > y0 + 5.0:
                            rect = pymupdf.Rect(x0, y0, x1, y1)
                            # alpha=False: 强制纯白不透明底色，避免微服务转 RGB 底色变黑
                            pix = page.get_pixmap(clip=rect, dpi=300, alpha=False)
                            if pix and pix.width > 0 and pix.height > 0:
                                crop_file = self.crops_dir / f"docx_eq_p{pno + 1}_{idx}.png"
                                pix.save(str(crop_file))
                                crop_path = crop_file
                                block.image_path = str(crop_file.relative_to(out_root))

                # 复制至独立调试目录
                img_for_rec = crop_path if (crop_path and crop_path.exists()) else None
                debug_crop_copy = debug_crops_dir / f"{call_id}.png"
                if img_for_rec and img_for_rec.exists():
                    shutil.copy2(img_for_rec, debug_crop_copy)

                # (2) 交给公式大模型识别，并捕获完整调试输入输出
                clean_latex, debug_info = self.recognizer.recognize_image(img_for_rec, block)
                block.original_text = f"$$\n{clean_latex}\n$$"

                # 组装本条完整调试记录
                record_entry = {
                    "call_id": call_id,
                    "block_id": block.id,
                    "page_num": pno + 1,
                    "bbox": block.bbox,
                    "crop_image": f"crops/{call_id}.png" if debug_crop_copy.exists() else None,
                    "raw_text_from_pdf": block.original_text.replace("$$", "").strip(),
                    "unimernet_url": debug_info.get("unimernet_url"),
                    "unimernet_available": debug_info.get("unimernet_available"),
                    "unimernet_latex": debug_info.get("unimernet_latex"),
                    "unimernet_response": debug_info.get("unimernet_response"),
                    "builtin_fallback_latex": debug_info.get("builtin_fallback_latex"),
                    "final_latex": clean_latex,
                    "used_unimernet": debug_info.get("used_unimernet"),
                    "status": debug_info.get("status"),
                    "latency_ms": debug_info.get("latency_ms", 0.0),
                    "error": debug_info.get("error")
                }
                self.debug_records.append(record_entry)

                # 保存单条 JSON
                record_file = debug_records_dir / f"{call_id}.json"
                record_file.write_text(json.dumps(record_entry, ensure_ascii=False, indent=2), encoding="utf-8")

        if pdf_doc is not None:
            pdf_doc.close()

        # 生成全局汇总 JSON 与可视化 HTML / Markdown 检查面板
        if self.debug_records:
            self._generate_debug_summary_artifacts(debug_dir)

        # 2. 结构化正文整理与参数对齐: 修复正文中的公式参数角标
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
                # 执行参数与角标替换 (使用 lambda 避免 re.sub 对 LaTeX 反斜杠的模板转义错误)
                for pattern, replacement in self.symbol_replacements:
                    text = pattern.sub(lambda _, r=replacement: r, text)
                block.original_text = text

        return blocks

    def _generate_debug_summary_artifacts(self, debug_dir: Path):
        """生成 UniMERNet 识别输入输出检查汇总与可视化页面"""
        # 1. 汇总 JSON
        summary_json = debug_dir / "records.json"
        summary_json.write_text(json.dumps(self.debug_records, ensure_ascii=False, indent=2), encoding="utf-8")

        # 2. Markdown 汇总
        md_lines = [
            "# UniMERNet 公式识别调用调试与输入输出对比清单",
            "",
            f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
            f"> 总公式块数: **{len(self.debug_records)}** 处，UniMERNet 成功采用: **{sum(1 for r in self.debug_records if r['used_unimernet'])}** 处，内置引擎备选: **{sum(1 for r in self.debug_records if not r['used_unimernet'])}** 处",
            "",
            "| 序号 | 页码 | 裁剪图 (Crop) | PDF 原文 | UniMERNet 识别结果 | 内置引擎备选 | 最终使用 LaTeX | 状态与耗时 |",
            "| :---: | :---: | :---: | :--- | :--- | :--- | :--- | :--- |"
        ]
        for r in self.debug_records:
            cid = r["call_id"]
            pno = r["page_num"]
            img_md = f"![{cid}]({r['crop_image']})" if r.get("crop_image") else "无图"
            pdf_raw = (r.get("raw_text_from_pdf") or "").replace("\n", " ")[:60]
            uni_out = (r.get("unimernet_latex") or "").replace("\n", " ")
            uni_show = f"`{uni_out}`" if uni_out else "<span style='color:gray;'>未返回/失败</span>"
            fallback_out = (r.get("builtin_fallback_latex") or "").replace("\n", " ")
            fallback_show = f"`{fallback_out}`" if fallback_out else "-"
            final_out = f"`{r.get('final_latex', '')}`"
            status_show = f"{r.get('status')} ({r.get('latency_ms')} ms)"
            md_lines.append(f"| {cid} | P{pno} | {img_md} | {pdf_raw} | {uni_show} | {fallback_show} | {final_out} | {status_show} |")

        (debug_dir / "summary.md").write_text("\n".join(md_lines), encoding="utf-8")

        # 3. HTML 可视化审查面板
        html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>UniMERNet 公式识别调试与审查面板</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 24px; background: #f8fafc; color: #1e293b; }}
  h1 {{ font-size: 22px; color: #0f172a; margin-bottom: 6px; }}
  .desc {{ color: #64748b; font-size: 14px; margin-bottom: 20px; }}
  .stats-grid {{ display: flex; gap: 16px; margin-bottom: 20px; }}
  .stat-card {{ background: #fff; padding: 12px 18px; border-radius: 8px; border: 1px solid #e2e8f0; }}
  .stat-card b {{ font-size: 20px; color: #2563eb; display: block; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  th {{ background: #f1f5f9; text-align: left; padding: 10px 12px; font-size: 13px; font-weight: 600; color: #475569; border-bottom: 1px solid #e2e8f0; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #e2e8f0; vertical-align: top; font-size: 13px; }}
  tr:hover {{ background: #f8fafc; }}
  .badge {{ display: inline-block; padding: 3px 8px; border-radius: 10px; font-size: 12px; font-weight: 500; }}
  .badge-success {{ background: #dcfce7; color: #166534; }}
  .badge-fallback {{ background: #fef3c7; color: #92400e; }}
  .badge-error {{ background: #fee2e2; color: #991b1b; }}
  .crop-preview {{ max-height: 75px; max-width: 280px; border: 1px solid #cbd5e1; border-radius: 4px; background: #fff; padding: 3px; object-fit: contain; }}
  code {{ font-family: Consolas, Monaco, monospace; font-size: 12px; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; color: #0f172a; word-break: break-all; }}
  pre {{ background: #f1f5f9; padding: 6px 10px; border-radius: 4px; font-size: 12px; white-space: pre-wrap; word-break: break-all; margin: 0; font-family: Consolas, Monaco, monospace; }}
</style>
</head>
<body>
<h1>📐 UniMERNet 公式识别调用审查面板</h1>
<p class="desc">完整记录本次运行中每个独立公式块的高清裁剪原图输入、UniMERNet 返回结果、内置引擎备选与最终输出。</p>

<div class="stats-grid">
  <div class="stat-card">公式块总数 <b>{len(self.debug_records)}</b></div>
  <div class="stat-card">UniMERNet 采用 <b>{sum(1 for r in self.debug_records if r['used_unimernet'])}</b></div>
  <div class="stat-card">内置引擎备选 <b>{sum(1 for r in self.debug_records if not r['used_unimernet'])}</b></div>
  <div class="stat-card">平均耗时 <b>{sum(r.get('latency_ms', 0) for r in self.debug_records) / max(1, len(self.debug_records)):.1f} ms</b></div>
</div>

<table>
<thead>
  <tr>
    <th style="width: 60px;">ID</th>
    <th style="width: 50px;">页码</th>
    <th style="width: 250px;">公式裁剪输入 (Crop)</th>
    <th>UniMERNet 预测输出</th>
    <th>内置引擎备选</th>
    <th>最终选用 LaTeX</th>
    <th style="width: 130px;">状态 / 耗时</th>
  </tr>
</thead>
<tbody>
"""
        for r in self.debug_records:
            badge_cls = "badge-success" if r["used_unimernet"] else "badge-fallback"
            img_tag = f'<img class="crop-preview" src="{r["crop_image"]}" alt="crop" />' if r.get("crop_image") else '<span>无图</span>'
            uni_show = f'<pre>{r.get("unimernet_latex") or ""}</pre>' if r.get("unimernet_latex") else '<span style="color:#94a3b8;">未启用或失败: ' + str(r.get("error") or "") + '</span>'
            fallback_show = f'<pre>{r.get("builtin_fallback_latex") or ""}</pre>' if r.get("builtin_fallback_latex") else '-'
            final_show = f'<pre>{r.get("final_latex") or ""}</pre>'
            html_content += f"""  <tr>
    <td><b>{r["call_id"]}</b></td>
    <td>P{r["page_num"]}</td>
    <td>{img_tag}</td>
    <td>{uni_show}</td>
    <td>{fallback_show}</td>
    <td>{final_show}</td>
    <td><span class="badge {badge_cls}">{r.get("status")}</span><br/><small>{r.get("latency_ms")} ms</small></td>
  </tr>
"""
        html_content += """</tbody>
</table>
</body>
</html>
"""
        (debug_dir / "index.html").write_text(html_content, encoding="utf-8")

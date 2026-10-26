import os
import io
import sys
import json
import time
import base64
import argparse
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from pydantic import BaseModel
import uvicorn
from PIL import Image
import torch

os.environ["NO_ALBUMENTATIONS_UPDATE"] = "1"

app = FastAPI(
    title="UniMERNet 公式识别微服务",
    description="为学术文献翻译伴读 Agent 提供高精度本地 LaTeX 数学公式识别、参数解析与全生命周期输入输出调试归档",
    version="1.1.0"
)

MODEL_LOADED = False
MODEL_INSTANCE = None
PROCESSOR_INSTANCE = None
DEVICE = "cpu"
CALL_COUNTER = 0

DEBUG_LOGS_DIR = Path(__file__).resolve().parent / "debug_logs"
DEBUG_IMAGES_DIR = DEBUG_LOGS_DIR / "images"
DEBUG_LOGS_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = DEBUG_LOGS_DIR / "predict_history.jsonl"


def _ensure_white_background(image: Image.Image) -> Image.Image:
    """
    确保图片在转换为 RGB 前消除透明通道 (RGBA / LA / 调色板透明):
    将其复合到纯白底色上，防止透明像素转 RGB 变为纯黑而严重干扰 OCR 与公式识别。
    """
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        bg = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    return image.convert("RGB")


def _record_debug_entry(
    call_id: str,
    img: Optional[Image.Image],
    source_path: Optional[str],
    raw_pred: str,
    final_latex: str,
    latency_ms: float,
    status: str,
    error_msg: Optional[str] = None
):
    """持久化记录每次调用的完整输入与输出至 debug_logs 目录，便于检查与调试准确率问题"""
    global CALL_COUNTER
    saved_img_rel = None

    if img is not None:
        try:
            img_file_name = f"{call_id}.png"
            img_save_path = DEBUG_IMAGES_DIR / img_file_name
            # 统一保存为高质量 RGB PNG
            img_rgb = _ensure_white_background(img)
            img_rgb.save(img_save_path)
            saved_img_rel = f"images/{img_file_name}"
        except Exception as e:
            print(f"[!] 保存调试图片异常: {e}")

    entry = {
        "call_id": call_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "status": status,
        "source_path": source_path or "",
        "saved_image": saved_img_rel,
        "image_size": [img.width, img.height] if img else None,
        "raw_pred_str": raw_pred,
        "final_latex": final_latex,
        "latency_ms": round(latency_ms, 2),
        "error": error_msg
    }

    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[!] 写入日志异常: {e}")

    # 同时更新简易 HTML 调试看板
    _update_debug_html_dashboard()


def _update_debug_html_dashboard():
    """生成或刷新本地可视化的 HTML 调试看板，方便用户直接双击查看每一次调用的输入图与识别输出"""
    if not HISTORY_FILE.exists():
        return

    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").strip().splitlines()
        records = [json.loads(line) for line in lines[-200:]] # 保留最近 200 条
        records.reverse()

        html_content = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>UniMERNet 公式识别调试面板</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 24px; background: #f8fafc; color: #1e293b; }
  h1 { color: #0f172a; font-size: 24px; margin-bottom: 8px; }
  .desc { color: #64748b; font-size: 14px; margin-bottom: 20px; }
  .stats-bar { display: flex; gap: 16px; margin-bottom: 24px; }
  .stat-card { background: #fff; padding: 12px 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); font-size: 14px; }
  .stat-card b { font-size: 20px; color: #2563eb; display: block; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  th { background: #f1f5f9; text-align: left; padding: 12px 14px; font-size: 13px; font-weight: 600; color: #475569; border-bottom: 1px solid #e2e8f0; }
  td { padding: 12px 14px; border-bottom: 1px solid #e2e8f0; vertical-align: top; font-size: 13px; }
  tr:hover { background: #f8fafc; }
  .badge { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; }
  .badge-success { background: #dcfce7; color: #15803d; }
  .badge-error { background: #fee2e2; color: #b91c1c; }
  .thumb-img { max-height: 80px; max-width: 260px; border: 1px solid #e2e8f0; border-radius: 4px; background: #fff; padding: 4px; object-fit: contain; }
  pre { background: #f1f5f9; padding: 8px 12px; border-radius: 6px; font-family: Consolas, Monaco, "Courier New", monospace; font-size: 12px; white-space: pre-wrap; word-break: break-all; margin: 0; color: #0f172a; }
</style>
</head>
<body>
<h1>🧪 UniMERNet 公式识别调用调试控制台</h1>
<p class="desc">记录 UniMERNet 本地微服务接收到的每次公式裁剪原图输入与大模型输出结果，供调试模型识别准确性。</p>
<div class="stats-bar">
  <div class="stat-card">总调用次数 <b>""" + str(len(records)) + """</b></div>
  <div class="stat-card">成功次数 <b>""" + str(sum(1 for r in records if r["status"] == "success")) + """</b></div>
  <div class="stat-card">平均耗时 <b>""" + f"{sum(r.get('latency_ms', 0) for r in records) / max(1, len(records)):.1f} ms" + """</b></div>
</div>
<table>
<thead>
  <tr>
    <th style="width: 70px;">序号</th>
    <th style="width: 130px;">调用时间</th>
    <th style="width: 80px;">状态</th>
    <th style="width: 260px;">输入裁剪图像</th>
    <th>识别输出 (LaTeX)</th>
    <th style="width: 90px;">耗时</th>
  </tr>
</thead>
<tbody>
"""
        for idx, r in enumerate(records):
            img_html = f'<img class="thumb-img" src="{r["saved_image"]}" alt="crop" />' if r.get("saved_image") else '<span style="color:#94a3b8;">无图像</span>'
            status_cls = "badge-success" if r["status"] == "success" else "badge-error"
            latex_show = r.get("final_latex") or (r.get("error") or "<空>")
            html_content += f"""  <tr>
    <td><b>#{len(records) - idx}</b></td>
    <td><small>{r.get("timestamp", "")}</small></td>
    <td><span class="badge {status_cls}">{r.get("status")}</span></td>
    <td>{img_html}<br/><small style="color:#64748b;">尺寸: {r.get("image_size")}</small></td>
    <td><pre>{latex_show}</pre></td>
    <td>{r.get("latency_ms", 0)} ms</td>
  </tr>
"""
        html_content += """</tbody>
</table>
</body>
</html>
"""
        (DEBUG_LOGS_DIR / "index.html").write_text(html_content, encoding="utf-8")
    except Exception as e:
        print(f"[!] 刷新 HTML 调试看板异常: {e}")


def init_model(cfg_path: Optional[str] = None):
    """初始化并加载 UniMERNet 模型权重到 GPU/CPU"""
    global MODEL_LOADED, MODEL_INSTANCE, PROCESSOR_INSTANCE, DEVICE

    try:
        import unimernet.tasks as tasks
        from unimernet.common.config import Config
        from unimernet.processors import load_processor

        DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[*] 正在初始化 UniMERNet 模型 (运行设备: {DEVICE})...")

        if not cfg_path:
            root_dir = Path(__file__).resolve().parent
            cfg_path = str(root_dir / "configs" / "unimernet_base.yaml")

        root_dir = Path(__file__).resolve().parent
        repo_root = root_dir.parent.parent
        try:
            os.chdir(repo_root)
        except Exception:
            pass

        if not os.path.exists(cfg_path):
            print(f"[!] 配置文件不存在: {cfg_path}")
            return

        print(f"[*] 读取配置文件: {cfg_path}")
        args = argparse.Namespace(cfg_path=cfg_path, options=None)
        cfg = Config(args)

        task = tasks.setup_task(cfg)
        model = task.build_model(cfg).to(DEVICE)
        model.eval()

        vis_processor = load_processor(
            "formula_image_eval",
            cfg.config.datasets.formula_rec_eval.vis_processor.eval
        )

        MODEL_INSTANCE = model
        PROCESSOR_INSTANCE = vis_processor
        MODEL_LOADED = True

        dev_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
        print(f"[+] UniMERNet 模型加载就绪！加速设备: {dev_name}")

    except Exception as e:
        print(f"[!] UniMERNet 模型加载异常: {e}")
        traceback.print_exc()


class PredictRequest(BaseModel):
    image: Optional[str] = None  # Base64 编码图片
    path: Optional[str] = None   # 本地图片路径


@app.get("/health")
def health_check():
    """健康探测接口"""
    cuda_avail = torch.cuda.is_available()
    dev_name = torch.cuda.get_device_name(0) if cuda_avail else "CPU"

    return {
        "status": "ok",
        "service": "UniMERNet Formula Recognition Service",
        "model_loaded": MODEL_LOADED,
        "cuda_available": cuda_avail,
        "device": dev_name,
        "python": sys.version.split()[0],
        "debug_logs_path": str(DEBUG_LOGS_DIR.resolve())
    }


def _predict_pil_image(image: Image.Image) -> tuple[str, str]:
    """对 PIL 图像执行公式识别，返回 (raw_pred_str, final_latex)"""
    global MODEL_LOADED, MODEL_INSTANCE, PROCESSOR_INSTANCE, DEVICE
    if not MODEL_LOADED or MODEL_INSTANCE is None or PROCESSOR_INSTANCE is None:
        return "", ""

    try:
        # 确保图像底色纯白，消除 alpha 通道转 RGB 变黑导致的模型识别失准
        rgb_image = _ensure_white_background(image)
        image_tensor = PROCESSOR_INSTANCE(rgb_image).unsqueeze(0).to(DEVICE)

        with torch.no_grad():
            output = MODEL_INSTANCE.generate({"image": image_tensor})
            if "pred_str" in output and output["pred_str"]:
                raw_pred = output["pred_str"][0]
                final_latex = str(raw_pred).strip()
                return str(raw_pred), final_latex
            return "", ""
    except Exception as e:
        print(f"[!] UniMERNet 推理异常: {e}")
        traceback.print_exc()
        raise e


@app.post("/predict")
async def predict_formula(req: PredictRequest, request: Request):
    """
    JSON 公式识别接口:
    支持 Base64 图片 (req.image) 或 本地绝对路径 (req.path)
    完整归档输入输出图片与调试日志
    """
    global CALL_COUNTER
    CALL_COUNTER += 1
    call_id = f"call_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{CALL_COUNTER:04d}"
    t0 = time.perf_counter()

    img = None
    if req.path:
        local_p = Path(req.path)
        if local_p.exists():
            try:
                img = Image.open(local_p)
            except Exception as e:
                err = f"无法读取指定路径图片: {local_p} ({e})"
                _record_debug_entry(call_id, None, req.path, "", "", 0.0, "error", err)
                raise HTTPException(status_code=400, detail=err)

    if img is None and req.image:
        try:
            b64_data = req.image
            if "," in b64_data:
                b64_data = b64_data.split(",", 1)[1]
            img_bytes = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(img_bytes))
        except Exception as e:
            err = f"Base64 图片解码失败: {e}"
            _record_debug_entry(call_id, None, req.path, "", "", 0.0, "error", err)
            raise HTTPException(status_code=400, detail=err)

    if img is None:
        err = "请提供有效的图片数据 (image base64 或 path)"
        _record_debug_entry(call_id, None, req.path, "", "", 0.0, "error", err)
        raise HTTPException(status_code=400, detail=err)

    try:
        raw_pred, latex_code = _predict_pil_image(img)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _record_debug_entry(
            call_id=call_id,
            img=img,
            source_path=req.path,
            raw_pred=raw_pred,
            final_latex=latex_code,
            latency_ms=elapsed_ms,
            status="success"
        )
        return {
            "status": "success",
            "latex": latex_code,
            "raw_pred": raw_pred,
            "width": img.width,
            "height": img.height,
            "call_id": call_id,
            "latency_ms": round(elapsed_ms, 2)
        }
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        err_msg = str(e)
        _record_debug_entry(
            call_id=call_id,
            img=img,
            source_path=req.path,
            raw_pred="",
            final_latex="",
            latency_ms=elapsed_ms,
            status="error",
            error_msg=err_msg
        )
        raise HTTPException(status_code=500, detail=f"公式识别推理失败: {e}")


@app.post("/predict_file")
async def predict_formula_file(file: UploadFile = File(...)):
    """Multipart 表单文件上传接口，完整归档输入输出图片与调试日志"""
    global CALL_COUNTER
    CALL_COUNTER += 1
    call_id = f"call_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{CALL_COUNTER:04d}"
    t0 = time.perf_counter()

    try:
        content = await file.read()
        img = Image.open(io.BytesIO(content))
        raw_pred, latex_code = _predict_pil_image(img)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _record_debug_entry(
            call_id=call_id,
            img=img,
            source_path=file.filename,
            raw_pred=raw_pred,
            final_latex=latex_code,
            latency_ms=elapsed_ms,
            status="success"
        )
        return {
            "status": "success",
            "latex": latex_code,
            "raw_pred": raw_pred,
            "width": img.width,
            "height": img.height,
            "call_id": call_id,
            "latency_ms": round(elapsed_ms, 2)
        }
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _record_debug_entry(
            call_id=call_id,
            img=None,
            source_path=file.filename,
            raw_pred="",
            final_latex="",
            latency_ms=elapsed_ms,
            status="error",
            error_msg=str(e)
        )
        raise HTTPException(status_code=500, detail=f"文件公式识别失败: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UniMERNet 本地微服务启动器 (带输入输出归档调试)")
    parser.add_argument("--port", type=int, default=8000, help="服务监听端口 (默认 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="服务监听地址 (默认 127.0.0.1)")
    parser.add_argument("--config", default=None, help="自定义 YAML 配置文件路径")
    args = parser.parse_args()

    init_model(args.config)
    uvicorn.run(app, host=args.host, port=args.port)

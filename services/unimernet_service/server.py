import os
import io
import sys
import base64
import argparse
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
import uvicorn
from PIL import Image
import torch

os.environ["NO_ALBUMENTATIONS_UPDATE"] = "1"

app = FastAPI(
    title="UniMERNet Formula Recognition Service",
    description="High-precision local LaTeX formula recognition and parameter parsing for Academic Translation Agent",
    version="1.0.0"
)

MODEL_LOADED = False
MODEL_INSTANCE = None
PROCESSOR_INSTANCE = None
DEVICE = "cpu"


def init_model(cfg_path: Optional[str] = None):
    """Initializes and loads UniMERNet model weights onto GPU/CPU"""
    global MODEL_LOADED, MODEL_INSTANCE, PROCESSOR_INSTANCE, DEVICE

    try:
        import unimernet.tasks as tasks
        from unimernet.common.config import Config
        from unimernet.processors import load_processor

        DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[*] Initializing UniMERNet model (device: {DEVICE})...")

        if not cfg_path:
            root_dir = Path(__file__).resolve().parent
            cfg_path = str(root_dir / "configs" / "unimernet_base.yaml")

        if not os.path.exists(cfg_path):
            print(f"[!] Configuration file not found: {cfg_path}")
            return

        print(f"[*] Reading config: {cfg_path}")
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
        print(f"[+] UniMERNet model loaded successfully on: {dev_name}")

    except Exception as e:
        print(f"[!] Failed to load UniMERNet model: {e}")
        import traceback
        traceback.print_exc()


class PredictRequest(BaseModel):
    image: Optional[str] = None  # Base64 encoded image
    path: Optional[str] = None   # Local image path


@app.get("/health")
def health_check():
    """Health check endpoint"""
    cuda_avail = torch.cuda.is_available()
    dev_name = torch.cuda.get_device_name(0) if cuda_avail else "CPU"

    return {
        "status": "ok",
        "service": "UniMERNet Formula Recognition Service",
        "model_loaded": MODEL_LOADED,
        "cuda_available": cuda_avail,
        "device": dev_name,
        "python": sys.version.split()[0]
    }


def _predict_pil_image(image: Image.Image) -> str:
    """Performs formula recognition on a PIL image"""
    global MODEL_LOADED, MODEL_INSTANCE, PROCESSOR_INSTANCE, DEVICE
    if not MODEL_LOADED or MODEL_INSTANCE is None or PROCESSOR_INSTANCE is None:
        return ""

    try:
        raw_image = image.convert("RGB")
        image_tensor = PROCESSOR_INSTANCE(raw_image).unsqueeze(0).to(DEVICE)

        with torch.no_grad():
            output = MODEL_INSTANCE.generate({"image": image_tensor})
            if "pred_str" in output and output["pred_str"]:
                pred = output["pred_str"][0]
                return str(pred).strip()
            return ""
    except Exception as e:
        print(f"[!] Error during inference: {e}")
        return ""


@app.post("/predict")
async def predict_formula(req: PredictRequest):
    """
    JSON formula prediction endpoint:
    Supports Base64 image (req.image) or absolute local path (req.path)
    """
    img = None
    if req.path:
        local_p = Path(req.path)
        if local_p.exists():
            img = Image.open(local_p)
    if img is None and req.image:
        try:
            b64_data = req.image
            if "," in b64_data:
                b64_data = b64_data.split(",", 1)[1]
            img_bytes = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(img_bytes))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to decode base64 image: {e}")

    if img is None:
        raise HTTPException(status_code=400, detail="Please provide valid image data (base64 image or path)")

    latex_code = _predict_pil_image(img)
    return {
        "status": "success",
        "latex": latex_code,
        "width": img.width,
        "height": img.height
    }


@app.post("/predict_file")
async def predict_formula_file(file: UploadFile = File(...)):
    """Multipart file upload endpoint"""
    content = await file.read()
    img = Image.open(io.BytesIO(content))
    latex_code = _predict_pil_image(img)
    return {
        "status": "success",
        "latex": latex_code,
        "width": img.width,
        "height": img.height
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UniMERNet Local Microservice Launcher")
    parser.add_argument("--port", type=int, default=8000, help="Listening port (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Listening address (default: 127.0.0.1)")
    parser.add_argument("--config", default=None, help="Custom YAML config path")
    args = parser.parse_args()

    init_model(args.config)
    uvicorn.run(app, host=args.host, port=args.port)

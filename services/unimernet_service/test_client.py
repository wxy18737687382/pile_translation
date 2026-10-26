import sys
import json
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

service_url = "http://127.0.0.1:8000"
health_url = f"{service_url}/health"
predict_url = f"{service_url}/predict"

print(f"[*] 正在探测 UniMERNet 微服务健康状态: {health_url}")
try:
    with urllib.request.urlopen(health_url, timeout=3.0) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        print("[+] 服务运行正常！")
        print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print(f"[-] 无法连接到服务: {e}")
    print("[!] 请确保先运行 start_service.bat 启动本地微服务。")
    sys.exit(1)

# 测试发送裁剪公式图片
sample_img = Path("output/images/docx_eq_p8_10.png")
if sample_img.exists():
    print(f"\n[*] 正在向服务发送测试公式图片: {sample_img}")
    import base64
    with open(sample_img, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    payload = json.dumps({"image": b64, "path": str(sample_img.resolve())}).encode("utf-8")
    req = urllib.request.Request(predict_url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        print("[+] 识别结果:")
        print(json.dumps(res, indent=2, ensure_ascii=False))

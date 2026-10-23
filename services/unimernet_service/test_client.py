import sys
import json
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

service_url = "http://127.0.0.1:8000"
health_url = f"{service_url}/health"
predict_url = f"{service_url}/predict"

print(f"[*] Checking UniMERNet microservice health status: {health_url}")
try:
    with urllib.request.urlopen(health_url, timeout=3.0) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        print("[+] Service is running!")
        print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print(f"[-] Could not connect to service: {e}")
    print("[!] Please ensure start_service.bat is running.")
    sys.exit(1)

# Test sending cropped formula image
sample_img = Path("output/images/docx_eq_p8_10.png")
if sample_img.exists():
    print(f"\n[*] Sending test formula image to service: {sample_img}")
    import base64
    with open(sample_img, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    payload = json.dumps({"image": b64, "path": str(sample_img.resolve())}).encode("utf-8")
    req = urllib.request.Request(predict_url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        print("[+] Recognition result:")
        print(json.dumps(res, indent=2, ensure_ascii=False))

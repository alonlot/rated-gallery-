"""One-time model download and TOPIQ-NR export. Run by install.bat. Needs internet; the app itself does not.

- InsightFace buffalo_l (SCRFD det_10g + ArcFace w600k_r50), MediaPipe Face Landmarker and the CLIP ViT-B/16
  image encoder are downloaded and checked with SHA-256.
- TOPIQ-NR is exported from IQA-PyTorch to ONNX in a temporary virtual environment with CPU PyTorch,
  which is deleted afterwards. The app never uses PyTorch.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import config

DOWNLOADS = [
    ("buffalo_l.zip", "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
     "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f"),
    ("face_landmarker.task",
     "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
     "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"),
    ("clip_vit_b16_vision.onnx", "https://huggingface.co/Xenova/clip-vit-base-patch16/resolve/main/onnx/vision_model.onnx",
     "b5170b47c0ecc667c9af5b42ed460c341377706d1466d177798c65fe49ea1e6b"),
]
FROM_ZIP = {"det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
            "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43"}
TOPIQ_WEIGHTS_SHA = "9a73138beb3748dd6a83365610ac88fa1cd53225342c77fc9ed44a3be461354c"

EXPORT_SCRIPT = r'''
import sys, torch, pyiqa
m = pyiqa.create_metric("topiq_nr", as_loss=False, device="cpu")
net = m.net.eval()
class W(torch.nn.Module):
    def __init__(s, n): super().__init__(); s.n = n
    def forward(s, x): return s.n(x).reshape(-1, 1)
w = W(net).eval()
x = torch.rand(1, 3, 384, 384)
with torch.no_grad(): ref = w(x)
torch.onnx.export(w, x, sys.argv[1], input_names=["image"], output_names=["score"], opset_version=17, dynamo=False)
import onnxruntime as ort, numpy as np
out = ort.InferenceSession(sys.argv[1], providers=["CPUExecutionProvider"]).run(None, {"image": x.numpy()})[0]
d = float(np.abs(out - ref.numpy()).max())
print("TOPIQ-NR export check: max difference", d)
sys.exit(0 if d < 1e-3 else 3)
'''


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def download(url: str, dst: Path, expect: str):
    if dst.exists() and sha256(dst) == expect:
        print(f"  ok      {dst.name}")
        return
    print(f"  get     {dst.name}  ({url})")
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            done += len(b)
            if total:
                print(f"\r          {done * 100 // total:3d}%", end="", flush=True)
    print()
    got = sha256(tmp)
    if got != expect:
        tmp.unlink()
        sys.exit(f"Checksum mismatch for {dst.name}: {got}")
    tmp.replace(dst)


def export_topiq(models: Path):
    out = models / "topiq_nr.onnx"
    if out.exists():
        print("  ok      topiq_nr.onnx")
        return
    print("  export  topiq_nr.onnx (temporary CPU PyTorch environment, a few minutes)")
    tmpdir = Path(tempfile.mkdtemp(prefix="rg-export-"))
    try:
        venv = tmpdir / "venv"
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
        py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        pip = [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
        subprocess.check_call(pip + ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"])
        subprocess.check_call(pip + ["pyiqa", "onnx", "onnxruntime"])
        script = tmpdir / "export.py"
        script.write_text(EXPORT_SCRIPT)
        env_home = tmpdir / "torch_home"
        import os

        env = dict(os.environ, TORCH_HOME=str(env_home))
        subprocess.check_call([str(py), str(script), str(out)], env=env)
        weights = list(env_home.rglob("cfanet_nr_koniq_res50*.pth"))
        if weights and sha256(weights[0]) != TOPIQ_WEIGHTS_SHA:
            out.unlink(missing_ok=True)
            sys.exit("TOPIQ weights checksum mismatch")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    models = config.MODELS
    models.mkdir(parents=True, exist_ok=True)
    print(f"Models folder: {models}")
    have_face = all((models / n).exists() and sha256(models / n) == h for n, h in FROM_ZIP.items())
    for name, url, sha in DOWNLOADS:
        if name == "buffalo_l.zip" and have_face:
            print("  ok      det_10g.onnx, w600k_r50.onnx")
            continue
        download(url, models / name, sha)
    if not have_face:
        with zipfile.ZipFile(models / "buffalo_l.zip") as z:
            for name, sha in FROM_ZIP.items():
                dst = models / name
                with z.open(name) as src, open(dst, "wb") as f:
                    shutil.copyfileobj(src, f)
                if sha256(dst) != sha:
                    sys.exit(f"Checksum mismatch for {name}")
                print(f"  ok      {name}")
        (models / "buffalo_l.zip").unlink()
    export_topiq(models)
    print("All models are ready. The app runs offline from now on.")


if __name__ == "__main__":
    main()

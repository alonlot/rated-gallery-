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
# SCUT-FBP5500 ResNet-18 (official weights, Python 3 copy). Non-commercial research terms of SCUT-FBP5500.
BEAUTY_URL = "https://huggingface.co/Gustrd/SCUT-FBP5500-PyTorch-Model/resolve/main/resnet18_py3.pth"
BEAUTY_SHA = "00d406a1d9bfb665ac760f260178b20034a0e4c954e01b19fc502012d486c8a4"

BEAUTY_SCRIPT = r'''
import re, sys
import numpy as np, torch, torchvision
# weights_only loading: no code from the file runs; only these NumPy data classes are allowed
allowed = [(np._core.multiarray.scalar, "numpy.core.multiarray.scalar"), np.dtype, np.dtypes.Float64DType, np.dtypes.Float32DType]
with torch.serialization.safe_globals(allowed):
    ck = torch.load(sys.argv[1], map_location="cpu", weights_only=True)
sd = {}
for k, v in ck.get("state_dict", ck).items():
    k = re.sub(r"^group1\.", "", k.replace("module.", ""))
    k = re.sub(r"(layer\d\.\d+)\.group1\.", r"\1.", k)
    sd[k.replace("group2.fullyconnected", "fc")] = v
m = torchvision.models.resnet18(num_classes=1)
m.load_state_dict(sd, strict=True)
m.eval()
x = torch.rand(1, 3, 224, 224)
with torch.no_grad(): ref = m(x)
torch.onnx.export(m, x, sys.argv[2], input_names=["image"], output_names=["beauty"], opset_version=17,
                  dynamic_axes={"image": {0: "batch"}, "beauty": {0: "batch"}}, dynamo=False)
import onnxruntime as ort
out = ort.InferenceSession(sys.argv[2], providers=["CPUExecutionProvider"]).run(None, {"image": x.numpy()})[0]
d = float(np.abs(out - ref.numpy()).max())
print("Beauty model export check: max difference", d)
sys.exit(0 if d < 1e-3 else 3)
'''

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


def export_with_torch(models: Path):
    """Export TOPIQ-NR and the SCUT-FBP5500 model to ONNX in a temporary CPU PyTorch environment."""
    need_iqa = not (models / "topiq_nr.onnx").exists()
    need_beauty = not (models / "beauty_scut.onnx").exists()
    if not need_iqa and not need_beauty:
        print("  ok      topiq_nr.onnx, beauty_scut.onnx")
        return
    print("  export  " + ", ".join(n for n, f in (("topiq_nr.onnx", need_iqa), ("beauty_scut.onnx", need_beauty)) if f)
          + " (temporary CPU PyTorch environment, a few minutes)")
    tmpdir = Path(tempfile.mkdtemp(prefix="rg-export-"))
    try:
        venv = tmpdir / "venv"
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
        py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        pip = [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
        subprocess.check_call(pip + ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"])
        subprocess.check_call(pip + ["pyiqa", "onnx", "onnxruntime"])
        import os

        env = dict(os.environ, TORCH_HOME=str(tmpdir / "torch_home"))
        if need_iqa:
            script = tmpdir / "export_iqa.py"
            script.write_text(EXPORT_SCRIPT)
            subprocess.check_call([str(py), "-I", str(script), str(models / "topiq_nr.onnx")], env=env)
            weights = list((tmpdir / "torch_home").rglob("cfanet_nr_koniq_res50*.pth"))
            if weights and sha256(weights[0]) != TOPIQ_WEIGHTS_SHA:
                (models / "topiq_nr.onnx").unlink(missing_ok=True)
                sys.exit("TOPIQ weights checksum mismatch")
        if need_beauty:
            pth = tmpdir / "beauty" / "resnet18_py3.pth"
            pth.parent.mkdir()
            download(BEAUTY_URL, pth, BEAUTY_SHA)
            script = tmpdir / "export_beauty.py"
            script.write_text(BEAUTY_SCRIPT)
            subprocess.check_call([str(py), "-I", str(script), str(pth), str(models / "beauty_scut.onnx")], env=env)
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
    export_with_torch(models)
    print("All models are ready. The app runs offline from now on.")


if __name__ == "__main__":
    main()

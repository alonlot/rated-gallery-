"""Train the general attractiveness model from MEBeauty, on this PC (run once by setup_models / install.bat).

MEBeauty (github.com/fbplab/MEBeauty-database): 2,550 photos of men and women of 6 ethnic groups, in normal
conditions, each rated by about 300 people on a 1..10 scale. Terms: non-commercial academic research only.
This app is private and non-commercial; never put the result into an app that is sold.

Method: every photo is processed exactly like a photo of me in the app (SCRFD face box -> face crop with margin
1.25 -> CLIP ViT-B/16 image embedding). A ridge regression maps the embedding to the human score. Result:
data/models/beauty_mebeauty.npz (512 weights + bias). At run time it costs one dot product per face.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

from . import config

ZIP_URL = "https://github.com/fbplab/MEBeauty-database/archive/refs/heads/main.zip"
SCORE_SHA = {"train_2022": "f1773c200477638f5080792752b13b9a8377f6efc42e8d9fb10c89d5184190d6",
             "val_2022": "f8a27a87cc70f2d001a289d631fe08cd4fef888e45756d852fa04df805923094",
             "test_2022": "46b58ea7abf241e4130fe22e1a11fb917ead8e48b59c60f6bd0f22a201b55fed"}
OUT_NAME = "beauty_mebeauty.npz"
ALPHAS = (0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0)
NOTICE = ("General attractiveness model: the MEBeauty dataset is for non-commercial academic research only.\n"
          "  It is used here only to train a private, non-commercial model on this PC. Set RG_SKIP_MEBEAUTY=1 to skip it.")


def head_path() -> Path:
    return config.MODELS / OUT_NAME


def load_head():
    p = head_path()
    if not p.exists():
        return None
    d = np.load(p)
    return d["w"].astype(np.float32), float(d["b"])


def _rows(z: zipfile.ZipFile, root: str):
    rows = []
    for split, sha in SCORE_SHA.items():
        raw = z.read(f"{root}scores/{split}.txt")
        if hashlib.sha256(raw).hexdigest() != sha:
            raise RuntimeError(f"MEBeauty score file {split} changed; not used")
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            rel, score = line.rsplit(" ", 1)
            sub = rel.split("images_crop_align_mtcnn/")[1]           # gender/ethnicity/file
            gender, eth = sub.split("/")[:2]
            rows.append((split, f"{root}original_images/{sub}", gender, eth, float(score)))
    return rows


def embed_rows(z, rows, prog=print):
    from . import imaging
    from .models import CLIP, SCRFD, clip_input

    det, clip = SCRFD(), CLIP()
    scut = None
    try:
        from .models import Beauty, missing_models

        if "beauty" not in missing_models():
            scut = Beauty()
    except Exception:  # noqa: BLE001
        scut = None
    E, keep, S = [], [], []
    names = set(z.namelist())
    for i, (split, path, gender, eth, score) in enumerate(rows):
        if path not in names:
            continue
        try:
            im, _ = imaging.decode(z.read(path), 1280)
        except Exception:  # noqa: BLE001
            continue
        arr = np.asarray(im)
        d, sc = imaging.letterbox(arr, 640)
        b, _, _ = det.detect(d)
        if not len(b):
            continue
        j = int(((b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])).argmax())
        bb = b[j] / sc
        H, W = arr.shape[:2]
        box = [bb[0] / W, bb[1] / H, bb[2] / W, bb[3] / H]
        E.append(clip.embed([clip_input(imaging.square_crop(im, box, 1.25, 256))])[0])
        keep.append((split, gender, eth, score))
        if scut is not None and split == "test_2022":  # the current model on the same test faces, for comparison
            S.append(scut.score([np.asarray(imaging.square_crop(im, box, 1.6, 256))])[0])
        else:
            S.append(np.nan)
        if i % 250 == 0:
            prog(f"          {i}/{len(rows)} faces")
    return np.array(E, np.float64), keep, np.array(S, np.float64)


def compare(pred, scut, keep):
    """Pearson correlation with human scores on the test faces: new head and SCUT model, all faces and Caucasian men."""
    from scipy.stats import pearsonr

    split = np.array([k[0] for k in keep])
    y = np.array([k[3] for k in keep])
    cm = np.array([k[1] == "male" and k[2] == "caucasian" for k in keep])
    out = {}
    for name, m in (("all", split == "test_2022"), ("caucasian_men", (split == "test_2022") & cm)):
        if m.sum() >= 10:
            out[f"head_{name}"] = float(pearsonr(pred[m], y[m]).statistic)
            ok = m & ~np.isnan(scut)
            if ok.sum() >= 10:
                out[f"scut_{name}"] = float(pearsonr(scut[ok], y[ok]).statistic)
    return out


def fit(E, keep):
    from scipy.stats import pearsonr

    split = np.array([k[0] for k in keep])
    y = np.array([k[3] for k in keep])
    tr, va, te = split == "train_2022", split == "val_2022", split == "test_2022"

    def ridge(X, t, a):
        mu = X.mean(0)
        Xc = X - mu
        w = np.linalg.solve(Xc.T @ Xc + a * np.eye(X.shape[1]), Xc.T @ (t - t.mean()))
        return w, t.mean() - mu @ w

    alpha = max(ALPHAS, key=lambda a: pearsonr(E[va] @ ridge(E[tr], y[tr], a)[0] + ridge(E[tr], y[tr], a)[1], y[va]).statistic)
    w, b = ridge(E[tr | va], y[tr | va], alpha)
    test_r = float(pearsonr(E[te] @ w + b, y[te]).statistic)
    return w.astype(np.float32), float(b), alpha, test_r, int(len(keep))


def main(prog=print) -> bool:
    out = head_path()
    if out.exists():
        prog("  ok      beauty_mebeauty.npz")
        return True
    prog("  " + NOTICE)
    tmp = Path(tempfile.mkdtemp(prefix="rg-mebeauty-"))
    try:
        zpath = tmp / "mebeauty.zip"
        prog("  get     MEBeauty dataset (about 450 MB, deleted after training)")
        with urllib.request.urlopen(ZIP_URL, timeout=120) as r, open(zpath, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        with zipfile.ZipFile(zpath) as z:
            root = z.namelist()[0].split("/")[0] + "/"
            rows = _rows(z, root)
            prog(f"  train   attractiveness model on {len(rows)} faces (a few minutes)")
            E, keep, S = embed_rows(z, rows, prog)
        w, b, alpha, test_r, n = fit(E, keep)
        c = compare(E @ w + b, S, keep)
        prog("  result  correlation with human scores on test faces (higher is better):")
        for k in ("all", "caucasian_men"):
            if f"head_{k}" in c:
                prog(f"          {k:14s} MEBeauty head {c[f'head_{k}']:.3f}" +
                     (f" | SCUT-FBP5500 {c[f'scut_{k}']:.3f}" if f"scut_{k}" in c else ""))
        key = "caucasian_men" if "scut_caucasian_men" in c else "all"
        if f"scut_{key}" in c and c[f"scut_{key}"] >= c[f"head_{key}"]:
            prog("  skip    MEBeauty head is not better than SCUT-FBP5500; the app keeps SCUT-FBP5500")
            return False
        if test_r < 0.5:
            prog(f"  skip    attractiveness model: test correlation {test_r:.2f} is too low")
            return False
        np.savez(out, w=w, b=np.float32(b), alpha=alpha, test_pearson=test_r, n=n, **{f"cmp_{k}": v for k, v in c.items()})
        prog(f"  ok      beauty_mebeauty.npz ({n} faces) is now the attractiveness model")
        return True
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

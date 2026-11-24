"""ONNX models on DirectML (CPU fallback per model), plus MediaPipe on CPU.

Each model is checked on DirectML first, in a child process (dml_probe.py), with the fixed shapes the app uses.
If that fails, the model runs on CPU. The device of each model is logged and shown in the UI.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from . import config

log = logging.getLogger("ratedgallery.models")

FILES = {
    "scrfd": "det_10g.onnx",
    "arcface": "w600k_r50.onnx",
    "clip": "clip_vit_b16_vision.onnx",
    "iqa": "topiq_nr.onnx",
    "beauty": "beauty_scut.onnx",
    "search_vision": "clip_b32_vision.onnx",
    "search_text": "mclip_text_q8.onnx",
    "search_dense": "mclip_dense.f32",
    "search_vocab": "mclip_vocab.txt",
    "landmarker": "face_landmarker.task",
    "hair": "hair_segmenter.tflite",
}

DEVICES: dict[str, dict] = {}
_sessions: dict[str, object] = {}
_lock = threading.Lock()


def model_path(name):
    return config.MODELS / FILES[name]


def missing_models():
    return [n for n, f in FILES.items() if not (config.MODELS / f).exists()]


def _providers():
    import onnxruntime as ort

    return ort.get_available_providers()


PROBE_FILE = "dml_probe.json"
_probe_lock = threading.Lock()


def _probe_key(name, feeds, dims) -> str:
    import onnxruntime as ort

    st = model_path(name).stat()
    return json.dumps([FILES[name], st.st_size, st.st_mtime_ns, ort.__version__, feeds, dims], sort_keys=True)


def _run_probe(name, feeds, dims) -> dict:
    """Run dml_probe.py in a child process. A crash there cannot break DirectML in this process."""
    spec = json.dumps({"path": str(model_path(name)), "feeds": feeds, "dims": dims})
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("dml_probe.py")), spec],
                           capture_output=True, text=True, timeout=300, creationflags=flags)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "DirectML check did not finish in 300 s"}
    for line in p.stdout.splitlines():
        if line.startswith("DML_PROBE "):
            return json.loads(line[len("DML_PROBE "):])
    return {"ok": False, "error": f"DirectML check crashed (exit code {p.returncode:#x})"}


def dml_probe(name, feeds, dims) -> dict:
    """Does this model work on DirectML with these fixed shapes? Checked once in a child process, then cached
    in data/models/dml_probe.json (new check after a new model file or ONNX Runtime version)."""
    path = config.MODELS / PROBE_FILE
    key = _probe_key(name, feeds, dims)
    with _probe_lock:
        try:
            cache = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            cache = {}
        if key in cache:
            return cache[key]
        t = time.perf_counter()
        res = _run_probe(name, feeds, dims)
        log.info("DirectML check of %s %s: %s (%.1f s)", name, feeds, "ok" if res.get("ok") else res.get("error"),
                 time.perf_counter() - t)
        cache[key] = res
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, indent=1), "utf-8")
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Could not save the DirectML check results: %s", e)
        return res


def _session(name, feeds: dict, dims: dict | None = None, tag=None):
    """ONNX Runtime session for one model. feeds: input name -> fixed shape (warm-up and DirectML check).
    dims: values for the free (named) dimensions of the model. DirectML gets these fixed shapes; some models
    (ArcFace) only work on DirectML with them. tag: one DirectML session per tag (for example per batch size);
    on the CPU all tags share one session."""
    import onnxruntime as ort

    dims = dims or {}
    feeds = {k: list(v) for k, v in feeds.items()}
    key = (name, tag)
    with _lock:
        if key in _sessions:
            return _sessions[key]
        path = str(model_path(name))
        avail = _providers()

        def warm(s):
            s.run(None, {k: np.zeros(v, np.float32) for k, v in feeds.items()})

        sess, err = None, None
        if "DmlExecutionProvider" in avail and not config.settings().get("force_cpu"):
            res = dml_probe(name, feeds, dims)
            if res.get("ok"):
                try:
                    so = ort.SessionOptions()
                    so.log_severity_level = 3
                    so.enable_mem_pattern = False
                    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                    for k, v in dims.items():
                        so.add_free_dimension_override_by_name(k, int(v))
                    s = ort.InferenceSession(path, so, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
                    warm(s)
                    sess, dev = s, "DirectML"
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}: {e}"[:300]
            else:
                err = res.get("error") or "DirectML check failed"
            if sess is None:
                log.warning("%s: DirectML failed, using CPU (%s)", name, err)
        if sess is None:
            sess = _sessions.get((name, "cpu"))
            if sess is None:
                so = ort.SessionOptions()
                so.log_severity_level = 3
                so.intra_op_num_threads = max(1, min(4, (config.settings()["workers"] or 4)))
                sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
                warm(sess)
                _sessions[(name, "cpu")] = sess
            dev = "CPU"
        if DEVICES.get(name, {}).get("device") != "CPU":  # a model counts as CPU if any of its sessions is
            DEVICES[name] = {"device": dev, "error": err, "providers": avail}
        log.info("Model %s%s runs on %s", name, f" ({tag})" if tag is not None else "", dev)
        _sessions[key] = sess
        return sess


def reset():
    with _lock:
        _sessions.clear()
        DEVICES.clear()


# ---------------------------------------------------------------- SCRFD face detector

class SCRFD:
    STRIDES = (8, 16, 32)

    def __init__(self, size=640):
        self.size = size
        self.inp = "input.1"
        self.sess = _session("scrfd", {self.inp: (1, 3, size, size)})
        self._centers = {}

    def _anchors(self, stride):
        if stride not in self._centers:
            n = self.size // stride
            yy, xx = np.mgrid[:n, :n]
            c = (np.stack([xx, yy], -1).reshape(-1, 2) * stride).astype(np.float32)
            self._centers[stride] = np.repeat(c, 2, axis=0)
        return self._centers[stride]

    def detect(self, img640: np.ndarray, thresh=0.5, nms=0.4):
        """img640: RGB uint8 (size x size). Returns boxes (n,4), scores (n,), kps (n,5,2) in pixels."""
        # (x - 127.5) / 128 as a contiguous NCHW array: a non-contiguous input costs about 4 ms more per run
        blob = cv2.dnn.blobFromImage(img640, 1 / 128.0, (img640.shape[1], img640.shape[0]), (127.5, 127.5, 127.5), swapRB=False)
        outs = self.sess.run(None, {self.inp: blob})
        boxes, scores, kpss = [], [], []
        for i, st in enumerate(self.STRIDES):
            sc = outs[i].reshape(-1)
            bb = outs[i + 3].reshape(-1, 4) * st
            kp = outs[i + 6].reshape(-1, 10) * st
            pos = np.where(sc >= thresh)[0]
            if not len(pos):
                continue
            c = self._anchors(st)[pos]
            b = bb[pos]
            boxes.append(np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1], c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], -1))
            k = kp[pos].reshape(-1, 5, 2) + c[:, None, :]
            kpss.append(k)
            scores.append(sc[pos])
        if not boxes:
            return np.zeros((0, 4)), np.zeros(0), np.zeros((0, 5, 2))
        boxes, scores, kpss = np.concatenate(boxes), np.concatenate(scores), np.concatenate(kpss)
        keep = _nms(boxes, scores, nms)
        return boxes[keep], scores[keep], kpss[keep]


def _nms(b, s, th):
    order = s.argsort()[::-1]
    keep = []
    areas = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(b[i, 0], b[order[1:], 0]); yy1 = np.maximum(b[i, 1], b[order[1:], 1])
        xx2 = np.minimum(b[i, 2], b[order[1:], 2]); yy2 = np.minimum(b[i, 3], b[order[1:], 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= th]
    return np.array(keep, dtype=int)


# ---------------------------------------------------------------- ArcFace

ARC_DST = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                    [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


def similarity_transform(src, dst):
    """Umeyama least squares similarity (2x3 matrix)."""
    src, dst = np.asarray(src, np.float64), np.asarray(dst, np.float64)
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s0, d0 = src - mu_s, dst - mu_d
    cov = d0.T @ s0 / len(src)
    U, S, Vt = np.linalg.svd(cov)
    D = np.eye(2)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        D[1, 1] = -1
    R = U @ D @ Vt
    scale = np.trace(np.diag(S) @ D) / (s0.var(0).sum() + 1e-12)
    t = mu_d - scale * R @ mu_s
    M = np.zeros((2, 3))
    M[:, :2] = scale * R
    M[:, 2] = t
    return M


def align_face(img: np.ndarray, kps: np.ndarray, size=112) -> np.ndarray:
    M = similarity_transform(kps, ARC_DST * (size / 112.0))
    return cv2.warpAffine(img, M, (size, size), borderValue=0.0)


class ArcFace:
    # DirectML, RX 6600 XT: batch 1 3.7 ms, 2 5.9 ms, 4 7.0 ms, 8 12.7 ms, 32 40 ms. Most photos have 1 to 3 faces,
    # so small batches matter; 32 is not faster per face than 4 x 8.
    BATCHES = (1, 2, 4, 8)

    def __init__(self):
        self.inp = "input.1"
        self._sess = {}
        self.session(8)

    def session(self, bs):
        """One session per batch size. The model's batch dimension ("None") must be fixed for DirectML:
        with a free batch size its last BatchNormalization fails on DirectML for batches > 1."""
        if bs not in self._sess:
            self._sess[bs] = _session("arcface", {self.inp: (bs, 3, 112, 112)}, {"None": bs}, tag=bs)
        return self._sess[bs]

    def embed(self, crops: list[np.ndarray]):
        """crops: aligned RGB 112x112 uint8. Returns (normalized embeddings (n,512), norms (n,))."""
        if not crops:
            return np.zeros((0, 512), np.float32), np.zeros(0, np.float32)
        out = []
        for i in range(0, len(crops), 8):
            chunk = crops[i:i + 8]
            n = len(chunk)
            bs = next(b for b in self.BATCHES if b >= n)  # fixed batch sizes help DirectML
            x = np.zeros((bs, 3, 112, 112), np.float32)
            x[:n] = ((np.stack(chunk).astype(np.float32) - 127.5) / 127.5).transpose(0, 3, 1, 2)
            out.append(self.session(bs).run(None, {self.inp: x})[0][:n])
        e = np.concatenate(out).astype(np.float32)
        norms = np.linalg.norm(e, axis=1)
        return e / np.maximum(norms[:, None], 1e-9), norms


# ---------------------------------------------------------------- CLIP image encoder

CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
CLIP_DIMS = {"batch_size": 8, "num_channels": 3, "height": 224, "width": 224}  # the app always runs batches of 8


class CLIP:
    def __init__(self):
        self.sess = _session("clip", {"pixel_values": (8, 3, 224, 224)}, CLIP_DIMS)

    def embed(self, imgs224: list[np.ndarray]):
        out = []
        for i in range(0, len(imgs224), 8):
            chunk = imgs224[i:i + 8]
            n = len(chunk)
            x = np.zeros((8, 3, 224, 224), np.float32)
            x[:n] = ((np.stack(chunk).astype(np.float32) / 255.0 - CLIP_MEAN) / CLIP_STD).transpose(0, 3, 1, 2)
            e = self.sess.run(["image_embeds"], {"pixel_values": x})[0][:n]
            out.append(e)
        if not out:
            return np.zeros((0, 512), np.float32)
        e = np.concatenate(out).astype(np.float32)
        return e / np.maximum(np.linalg.norm(e, axis=1, keepdims=True), 1e-9)


def clip_input(im) -> np.ndarray:
    """PIL image -> 224x224 uint8 (short side resize + center crop, as CLIP training)."""
    from PIL import Image

    w, h = im.size
    s = 224 / min(w, h)
    im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.Resampling.BICUBIC)
    w, h = im.size
    l, t = (w - 224) // 2, (h - 224) // 2
    return np.asarray(im.crop((l, t, l + 224, t + 224)), dtype=np.uint8)


# ---------------------------------------------------------------- TOPIQ-NR (image quality)

class IQA:
    SIZE = 384

    def __init__(self):
        self.sess = _session("iqa", {"image": (1, 3, 384, 384)})

    def score(self, imgs384: list[np.ndarray]):
        out = []
        for a in imgs384:
            x = (a.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
            out.append(float(self.sess.run(None, {"image": x})[0].reshape(-1)[0]))
        return out


# ---------------------------------------------------------------- general attractiveness model (SCUT-FBP5500)

IMNET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
IMNET_STD = np.array([0.229, 0.224, 0.225], np.float32)


class Beauty:
    """ResNet-18 trained on SCUT-FBP5500 (scale 1..5). Used only as ONE input of the personal model.

    Input: the cached 256 px face crop (square, margin 1.6). The score is the mean of 4 views
    (margin 1.6 resized, margin 1.4 center crop, and both mirrored), because one crop is unstable.
    """

    def __init__(self):
        self.sess = _session("beauty", {"image": (4, 3, 224, 224)}, {"batch": 4})

    @staticmethod
    def views(crop256: np.ndarray):
        a = cv2.resize(crop256, (224, 224), interpolation=cv2.INTER_AREA)
        o = (crop256.shape[0] - 224) // 2
        b = crop256[o:o + 224, o:o + 224]
        return [a, a[:, ::-1], b, b[:, ::-1]]

    def score(self, crops256: list[np.ndarray]) -> list[float]:
        out = []
        for c in crops256:
            v = np.stack(self.views(c)).astype(np.float32) / 255.0
            x = ((v - IMNET_MEAN) / IMNET_STD).transpose(0, 3, 1, 2).copy()
            out.append(float(self.sess.run(None, {"image": x})[0].mean()))
        return out


# ---------------------------------------------------------------- MediaPipe (CPU, per worker process)

BLENDSHAPES = ["eyeBlinkLeft", "eyeBlinkRight", "eyeSquintLeft", "eyeSquintRight", "mouthSmileLeft",
               "mouthSmileRight", "jawOpen", "browInnerUp", "browDownLeft", "browDownRight",
               "browOuterUpLeft", "browOuterUpRight", "mouthFrownLeft", "mouthFrownRight", "eyeWideLeft",
               "eyeWideRight", "mouthPucker"]


class Landmarker:
    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self.mp = mp
        opts = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_buffer=model_path("landmarker").read_bytes(),
                                     delegate=BaseOptions.Delegate.CPU),
            running_mode=vision.RunningMode.IMAGE, num_faces=1,
            output_face_blendshapes=True, output_facial_transformation_matrixes=True,
            min_face_detection_confidence=0.3, min_face_presence_confidence=0.3)
        self.lm = vision.FaceLandmarker.create_from_options(opts)

    def analyze(self, rgb: np.ndarray):
        """Returns dict of blendshape scores + yaw/pitch/roll (degrees), or None if no face found."""
        img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        res = self.lm.detect(img)
        if not res.face_blendshapes:
            return None
        bs = {c.category_name: float(c.score) for c in res.face_blendshapes[0]}
        out = {k: bs.get(k, 0.0) for k in BLENDSHAPES}
        if res.facial_transformation_matrixes:
            R = np.asarray(res.facial_transformation_matrixes[0])[:3, :3]
            out["pitch"] = float(np.degrees(np.arctan2(R[2, 1], R[2, 2])))
            out["yaw"] = float(np.degrees(np.arcsin(np.clip(-R[2, 0], -1, 1))))
            out["roll"] = float(np.degrees(np.arctan2(R[1, 0], R[0, 0])))
        return out


def pose_from_kps(kps: np.ndarray):
    """Rough yaw/pitch/roll in degrees from the 5 SCRFD points (fallback when MediaPipe fails)."""
    le, re_, nose, lm, rm = kps
    eye_mid = (le + re_) / 2
    mouth_mid = (lm + rm) / 2
    ed = np.linalg.norm(re_ - le) + 1e-6
    roll = float(np.degrees(np.arctan2(re_[1] - le[1], re_[0] - le[0])))
    yaw = float(np.clip((nose[0] - eye_mid[0]) / ed, -1, 1) * 60)
    span = np.linalg.norm(mouth_mid - eye_mid) + 1e-6
    pitch = float(np.clip(((nose[1] - eye_mid[1]) / span - 0.55) * 90, -60, 60))
    return yaw, pitch, roll


def timeit(fn, n=5):
    fn()
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n


# ---------------------------------------------------------------- hair (MediaPipe hair segmenter, Apache 2.0, CPU)

def head_box(box):
    """Square-ish box around the whole head (face box widened, moved up for the hair). Normalized x1,y1,x2,y2."""
    x1, y1, x2, y2 = box
    cx, cy, fw, fh = (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1
    return [cx - 1.25 * fw, cy - 1.35 * fh, cx + 1.25 * fw, cy + 0.65 * fh]


class HairSegmenter:
    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self.mp = mp
        opts = vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_buffer=model_path("hair").read_bytes(), delegate=BaseOptions.Delegate.CPU),
            running_mode=vision.RunningMode.IMAGE, output_confidence_masks=True, output_category_mask=False)
        self.seg = vision.ImageSegmenter.create_from_options(opts)

    def mask(self, rgb512: np.ndarray) -> np.ndarray:
        r = self.seg.segment(self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb512)))
        return r.confidence_masks[-1].numpy_view()[..., 0] > 0.5


def hair_measures(mask: np.ndarray) -> dict:
    """Hair values from a hair mask of the head crop made with head_box() (the face is at a fixed place in it).

    hair_area: hair area / face area. hair_rough: outline length / outline of its convex hull (1 = smooth, more =
    messy or flyaway). hair_over_face: part of the upper face (forehead, eyes) covered by hair. hair_lr: hair pushed
    to one side (0 = balanced). Values are NaN when little hair is visible (very short hair, hat): then they do not count.
    """
    S = mask.shape[0]
    # the face box inside the head crop: width 1/2.5 of the crop, from y = 0.35/2.0*1.0 ... see head_box()
    fx1, fx2 = S * (1.25 - 0.5) / 2.5, S * (1.25 + 0.5) / 2.5
    fy1, fy2 = S * (1.35 - 0.5) / 2.0, S * (1.35 + 0.5) / 2.0
    m = mask.copy()
    m[int(fy2 + 0.1 * (fy2 - fy1)):, :] = False          # below the chin: clothes, not hair
    face_area = (fx2 - fx1) * (fy2 - fy1)
    area = float(m.sum())
    out = {"hair_present": 1.0 if area >= 0.05 * face_area else 0.0}
    nan = float("nan")
    if not out["hair_present"]:
        return {**out, "hair_area": nan, "hair_rough": nan, "hair_over_face": nan, "hair_lr": nan}
    out["hair_area"] = area / face_area
    cnts, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    perim = sum(cv2.arcLength(c, True) for c in cnts)
    hull = cv2.convexHull(np.vstack(cnts))
    out["hair_rough"] = float(perim / max(cv2.arcLength(hull, True), 1.0))
    upper = m[int(fy1):int(fy1 + 0.5 * (fy2 - fy1)), int(fx1):int(fx2)]
    out["hair_over_face"] = float(upper.mean()) if upper.size else 0.0
    cx = S / 2
    left, right = float(m[:, :int(cx)].sum()), float(m[:, int(cx):].sum())
    out["hair_lr"] = abs(left - right) / max(left + right, 1.0)
    return out

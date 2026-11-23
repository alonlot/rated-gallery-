"""ONNX models on DirectML (CPU fallback per model), plus MediaPipe on CPU.

Each model is tried on DirectML first with a warm-up run. If that fails, the model runs on CPU.
The device of each model is logged and shown in the UI.
"""
from __future__ import annotations

import logging
import threading
import time

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
    "landmarker": "face_landmarker.task",
}

DEVICES: dict[str, dict] = {}
_sessions: dict[str, object] = {}
_lock = threading.Lock()


def model_path(name):
    return config.MODELS / FILES[name]


def missing_models():
    return [n for n, f in FILES.items() if not (config.MODELS / f).exists()]


def _session(name, warm):
    import onnxruntime as ort

    with _lock:
        if name in _sessions:
            return _sessions[name]
        path = str(model_path(name))
        avail = ort.get_available_providers()
        sess, err = None, None
        if "DmlExecutionProvider" in avail and not config.settings().get("force_cpu"):
            try:
                so = ort.SessionOptions()
                so.log_severity_level = 3
                so.enable_mem_pattern = False
                so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                s = ort.InferenceSession(path, so, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
                warm(s)
                sess, dev = s, "DirectML"
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:300]
                log.warning("%s: DirectML failed, using CPU (%s)", name, err)
        if sess is None:
            so = ort.SessionOptions()
            so.log_severity_level = 3
            so.intra_op_num_threads = max(1, min(4, (config.settings()["workers"] or 4)))
            sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
            warm(sess)
            dev = "CPU"
        DEVICES[name] = {"device": dev, "error": err, "providers": avail}
        log.info("Model %s runs on %s", name, dev)
        _sessions[name] = sess
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
        self.sess = _session("scrfd", lambda s: s.run(None, {s.get_inputs()[0].name: np.zeros((1, 3, size, size), np.float32)}))
        self.inp = self.sess.get_inputs()[0].name
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
        blob = ((img640.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]
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
    BATCHES = (1, 8, 32)

    def __init__(self):
        self.sess = _session("arcface", lambda s: s.run(None, {s.get_inputs()[0].name: np.zeros((8, 3, 112, 112), np.float32)}))
        self.inp = self.sess.get_inputs()[0].name

    def embed(self, crops: list[np.ndarray]):
        """crops: aligned RGB 112x112 uint8. Returns (normalized embeddings (n,512), norms (n,))."""
        if not crops:
            return np.zeros((0, 512), np.float32), np.zeros(0, np.float32)
        out = []
        for i in range(0, len(crops), 32):
            chunk = crops[i:i + 32]
            n = len(chunk)
            bs = next(b for b in self.BATCHES if b >= n)  # fixed batch sizes help DirectML
            x = np.zeros((bs, 3, 112, 112), np.float32)
            x[:n] = ((np.stack(chunk).astype(np.float32) - 127.5) / 127.5).transpose(0, 3, 1, 2)
            out.append(self.sess.run(None, {self.inp: x})[0][:n])
        e = np.concatenate(out).astype(np.float32)
        norms = np.linalg.norm(e, axis=1)
        return e / np.maximum(norms[:, None], 1e-9), norms


# ---------------------------------------------------------------- CLIP image encoder

CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)


class CLIP:
    def __init__(self):
        self.sess = _session("clip", lambda s: s.run(None, {"pixel_values": np.zeros((8, 3, 224, 224), np.float32)}))

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
        self.sess = _session("iqa", lambda s: s.run(None, {"image": np.zeros((1, 3, 384, 384), np.float32)}))

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
        self.sess = _session("beauty", lambda s: s.run(None, {"image": np.zeros((4, 3, 224, 224), np.float32)}))

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

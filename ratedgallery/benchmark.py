"""Startup benchmark: speed of decoding (one CPU worker) and of each model, as photos per minute."""
from __future__ import annotations

import io
import logging
import time

import numpy as np
from PIL import Image

from . import config, db, imaging, models

log = logging.getLogger("ratedgallery.benchmark")
MAIN_LOOP_MS = 8.0


def _synthetic_jpeg(w=4000, h=3000) -> bytes:
    rng = np.random.default_rng(0)
    small = rng.integers(0, 255, (h // 50, w // 50, 3), dtype=np.uint8)
    im = Image.fromarray(small).resize((w, h), Image.Resampling.BICUBIC)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return b.getvalue()


def _t(fn, n):
    fn()
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n


def run() -> dict:
    res = {"time": time.time()}
    data = _synthetic_jpeg()

    def decode():
        im, _ = imaging.decode(data, 1280)
        imaging.phash_hex(im)
        t = im.copy()
        t.thumbnail((400, 400))
        t.save(io.BytesIO(), "WEBP", quality=72, method=4)

    res["decode_ms"] = round(_t(decode, 4) * 1000, 1)
    workers = int(config.settings()["workers"])
    if models.missing_models():
        res["error"] = "models missing: " + ", ".join(models.missing_models())
        db.kv_set("benchmark", res)
        return res
    det = models.SCRFD()
    img = np.zeros((640, 640, 3), np.uint8)
    res["detect_ms"] = round(_t(lambda: det.detect(img), 10) * 1000, 1)
    arc = models.ArcFace()
    faces = [np.zeros((112, 112, 3), np.uint8)] * 8
    res["arcface_ms_per_face"] = round(_t(lambda: arc.embed(faces), 5) * 1000 / 8, 2)
    # a typical photo has about 2 faces; small batches cost more per face than a batch of 8
    res["arcface_ms_per_photo"] = round(_t(lambda: arc.embed(faces[:2]), 5) * 1000, 2)
    clip = models.CLIP()
    ims = [np.zeros((224, 224, 3), np.uint8)] * 8
    res["clip_ms_per_image"] = round(_t(lambda: clip.embed(ims), 3) * 1000 / 8, 1)
    iqa = models.IQA()
    q = [np.zeros((384, 384, 3), np.uint8)]
    res["iqa_ms"] = round(_t(lambda: iqa.score(q), 3) * 1000, 1)
    cpu_rate = workers * 60000 / res["decode_ms"]
    # main loop per photo: detection + ArcFace + about 8 ms of database work, duplicate index and receiving the
    # image from the worker (measured on a Ryzen 5 5600X; without it the estimate was 2 times too high)
    gpu_rate = 60000 / (res["detect_ms"] + res["arcface_ms_per_photo"] + MAIN_LOOP_MS)
    res["scan_photos_per_min"] = round(min(cpu_rate, gpu_rate))
    res["scan_limit"] = "CPU decoding" if cpu_rate < gpu_rate else "face detection"
    # pass 2 per photo: decode at higher size (~2x pass 1) + MediaPipe (~25 ms) on CPU; CLIP x2 + IQA on GPU
    cpu2 = workers * 60000 / (res["decode_ms"] * 2 + 25)
    gpu2 = 60000 / (2 * res["clip_ms_per_image"] + res["iqa_ms"])
    res["features_photos_per_min"] = round(min(cpu2, gpu2))
    res["workers"] = workers
    res["devices"] = {k: v["device"] for k, v in models.DEVICES.items()}
    db.kv_set("benchmark", res)
    log.info("Benchmark: %s", res)
    return res

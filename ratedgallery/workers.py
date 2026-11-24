"""CPU worker processes: decode, metadata, thumbnails, hashes (pass 1); face crop, MediaPipe,
sharpness, exposure and model inputs (pass 2). The GPU models run in the main process."""
from __future__ import annotations

import hashlib
import logging
import multiprocessing as mp
import os
import sys
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import numpy as np

from . import config, imaging, metadata, offline

log = logging.getLogger("ratedgallery.workers")
_landmarker = None
_hair = None
_lm_error_logged = False


def set_priority(pid=None, low=False):
    try:
        import psutil

        p = psutil.Process(pid)
        if sys.platform == "win32":
            p.nice(psutil.IDLE_PRIORITY_CLASS if low else psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(19 if low else 5)
    except Exception:  # noqa: BLE001
        pass


def init_worker(low: bool, data_dir: str, models_dir: str):
    offline.enable()
    config.DATA = Path(data_dir)
    config.MODELS = Path(models_dir)
    set_priority(low=low)
    try:
        import cv2

        cv2.setNumThreads(1)
    except Exception:  # noqa: BLE001
        pass


def thumb_rel(sha: str) -> str:
    return f"{sha[:2]}/{sha[2:20]}.webp"


# ---------------------------------------------------------------- pass 1

def scan_task(job: dict) -> dict:
    fid = job["file_id"]
    try:
        data = Path(job["path"]).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        im, info = imaging.decode(data, 1280)
        ex = metadata.parse_exif(info["exif"])
        side = metadata.read_sidecar(job.get("sidecar"))
        meta = metadata.merge(ex, side, job.get("taken_hint"), job.get("name"))
        rel = thumb_rel(sha)
        tp = config.cache_dir("thumbs") / rel
        if not tp.exists():
            imaging.save_webp(im, tp, 400)
        arr = np.asarray(im, dtype=np.uint8)
        det, scale = imaging.letterbox(arr, 640)
        return {"ok": True, "file_id": fid, "sha": sha, "size": len(data), "width": info["width"],
                "height": info["height"], "meta": meta, "phash": imaging.phash_hex(im), "thumb": rel,
                "img": arr, "det": det, "det_scale": scale}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "file_id": fid, "error": f"{type(e).__name__}: {e}"[:400]}


# ---------------------------------------------------------------- pass 2

def _lm():
    global _landmarker
    if _landmarker is None:
        from .models import Landmarker

        _landmarker = Landmarker()
    return _landmarker


def _hairseg():
    global _hair
    if _hair is None:
        from .models import HairSegmenter

        _hair = HairSegmenter()
    return _hair


def features_task(job: dict) -> dict:
    """job: photo_id, face_id, path, box (normalized x1,y1,x2,y2), kps (normalized 5x2), preview (path or None)."""
    import cv2
    from PIL import Image

    from .models import BLENDSHAPES, clip_input, pose_from_kps

    try:
        data = Path(job["path"]).read_bytes()
        box = job["box"]
        crop, full, info = imaging.decode_region(data, box, 512, 1.8)
        out = {"ok": True, "photo_id": job["photo_id"], "face_id": job["face_id"]}
        if job.get("preview"):
            out["preview_bytes"] = imaging.save_jpeg(full, Path(job["preview"]), 1600, 85)
        W, H = full.size
        x1, y1, x2, y2 = box
        f = {}
        r = None
        try:
            r = _lm().analyze(np.asarray(crop))
        except Exception as e:  # noqa: BLE001
            global _lm_error_logged
            if not _lm_error_logged:
                logging.getLogger("ratedgallery.workers").error("MediaPipe failed: %s", e)
                _lm_error_logged = True
            r = None
        f["mp_ok"] = 1.0 if r else 0.0
        kps = np.asarray(job["kps"], np.float64) * [W, H]
        if r and "yaw" in r:
            f["yaw"], f["pitch"], f["roll"] = r["yaw"], r["pitch"], r["roll"]
        else:
            f["yaw"], f["pitch"], f["roll"] = pose_from_kps(kps)
        for k in BLENDSHAPES:
            f[k] = r.get(k, 0.0) if r else float("nan")
        # tight face crop (bbox) for sharpness and exposure
        bx = (max(0, int(x1 * W)), max(0, int(y1 * H)), min(W, int(x2 * W)), min(H, int(y2 * H)))
        tight = np.asarray(full.crop(bx).convert("L"), dtype=np.float32)
        if tight.size < 16:
            raise ValueError("face crop too small")
        th, tw = tight.shape
        t160 = cv2.resize(tight, (160, max(8, int(160 * th / tw))), interpolation=cv2.INTER_AREA)
        f["sharp"] = float(np.log1p(cv2.Laplacian(t160, cv2.CV_32F).var()))
        lum = tight / 255.0
        f["face_mean"] = float(lum.mean())
        f["face_std"] = float(lum.std())
        f["clip_lo"] = float((lum < 0.02).mean())
        f["clip_hi"] = float((lum > 0.98).mean())
        small = full.convert("L").copy()
        small.thumbnail((256, 256))
        s = np.asarray(small, dtype=np.float32) / 255.0
        sh, sw = s.shape
        mask = np.ones_like(s, dtype=bool)
        cx, cy, hw, hh = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) * 0.65, (y2 - y1) * 0.65
        mask[max(0, int((cy - hh) * sh)):int((cy + hh) * sh) + 1, max(0, int((cx - hw) * sw)):int((cx + hw) * sw) + 1] = False
        bg = float(s[mask].mean()) if mask.any() else f["face_mean"]
        f["bg_mean"] = bg
        f["bg_contrast"] = f["face_mean"] - bg
        f["face_frac"] = float((x2 - x1) * (y2 - y1))
        f["face_w"] = float(x2 - x1)
        # hair: head crop around the face (the face is at a fixed place in it), hair mask, simple measures
        from .models import hair_measures, head_box

        head = imaging.square_crop(full, head_box(box), 1.0, 512)
        try:
            f.update(hair_measures(_hairseg().mask(np.asarray(head))))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("ratedgallery.workers").warning("hair segmenter failed: %s", e)
            f.update(hair_present=float("nan"), hair_area=float("nan"), hair_rough=float("nan"),
                     hair_over_face=float("nan"), hair_lr=float("nan"))
        f["hair_v"] = 1
        out["features"] = f
        out["clip_head"] = clip_input(head)
        out["clip_face"] = clip_input(imaging.square_crop(full, box, 1.25, 256))
        out["clip_full"] = clip_input(full)
        out["iqa_in"] = np.asarray(full.resize((384, 384), Image.Resampling.BICUBIC), dtype=np.uint8)
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "photo_id": job["photo_id"], "face_id": job["face_id"],
                "error": f"{type(e).__name__}: {e}"[:400], "trace": traceback.format_exc()[-800:]}


# ---------------------------------------------------------------- pool

class Pool:
    """Process pool with a bounded number of jobs in flight. Survives a crashed worker."""

    def __init__(self, n: int | None = None, low: bool | None = None):
        s = config.settings()
        self.low = s["low_priority"] if low is None else low
        n = n or int(s["workers"])
        if self.low:
            n = max(1, n // 2)
        self.n = max(1, n)
        self.ex = None
        self._start()

    def _start(self):
        ctx = mp.get_context("spawn")
        self.ex = ProcessPoolExecutor(self.n, mp_context=ctx, initializer=init_worker,
                                      initargs=(self.low, str(config.DATA), str(config.MODELS)))

    def _restart(self):
        try:
            self.ex.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        self._start()

    def pids(self):
        try:
            return list(self.ex._processes.keys())  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return []

    def set_low(self, low: bool):
        for pid in self.pids():
            set_priority(pid, low)

    def imap(self, fn, jobs, window: int | None = None):
        window = window or self.n * 3
        it = iter(jobs)
        pending: dict = {}
        attempts: dict = {}

        restarts = [0]

        def submit(j):
            try:
                return self.ex.submit(fn, j)
            except BrokenProcessPool:
                restarts[0] += 1
                if restarts[0] > 5:
                    raise RuntimeError("CPU worker processes keep crashing at start") from None
                self._restart()
                return self.ex.submit(fn, j)

        def fill():
            while len(pending) < window:
                j = next(it, None)
                if j is None:
                    return
                pending[submit(j)] = j

        fill()
        while pending:
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            retry = []
            for fut in done:
                j = pending.pop(fut)
                try:
                    yield fut.result()
                except BrokenProcessPool:
                    attempts[id(j)] = attempts.get(id(j), 0) + 1
                    if attempts[id(j)] >= 2:
                        yield {"ok": False, "error": "worker crashed on this file",
                               **{k: j.get(k) for k in ("file_id", "photo_id", "face_id")}}
                    else:
                        retry.append(j)
            if retry:
                log.warning("A worker process crashed; restarting the pool")
                retry += list(pending.values())
                pending.clear()
                self._restart()
                for j in retry:
                    pending[submit(j)] = j
            fill()

    def close(self):
        if self.ex:
            self.ex.shutdown(wait=True, cancel_futures=True)
            self.ex = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

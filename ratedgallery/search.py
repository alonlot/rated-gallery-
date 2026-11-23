"""Photo search by text, in 50+ languages (Hebrew and English included), fully offline.

- Image side: CLIP ViT-B/32 image encoder on the thumbnail of every photo (vectors in table photo_vec).
- Text side: sentence-transformers clip-ViT-B-32-multilingual-v1 (multilingual DistilBERT, int8),
  mean pooling, then a 768->512 linear layer into the CLIP ViT-B/32 space.
The phone app uses the same text model files and a Kotlin copy of the WordPiece tokenizer below
(both tested against tests/data/mclip_tokens.json, made with the official `tokenizers` library).
"""
from __future__ import annotations

import threading
import time
import unicodedata

import numpy as np
from PIL import Image

from . import config, db

CLS, SEP, UNK = 101, 102, 100
MAX_LEN = 128
MODEL_VERSION = "mclip-b32-v1"
TEXT_FILES = {"text": "mclip_text_q8.onnx", "dense": "mclip_dense.f32", "vocab": "mclip_vocab.txt"}

_CJK = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF), (0x2A700, 0x2B73F), (0x2B740, 0x2B81F),
        (0x2B820, 0x2CEAF), (0xF900, 0xFAFF), (0x2F800, 0x2FA1F))


# ---------------------------------------------------------------- WordPiece tokenizer (BERT, cased)

def _is_control(ch):
    if ch in "\t\n\r":
        return False
    return unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn")


def _is_punct(ch):
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(cp):
    return any(a <= cp <= b for a, b in _CJK)


def basic_tokens(text: str) -> list[str]:
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 0 or cp == 0xFFFD or _is_control(ch):
            continue
        if ch.isspace():
            out.append(" ")
        elif _is_cjk(cp):
            out.append(f" {ch} ")
        else:
            out.append(ch)
    words = []
    for w in "".join(out).split():
        cur = ""
        for ch in w:
            if _is_punct(ch):
                if cur:
                    words.append(cur)
                    cur = ""
                words.append(ch)
            else:
                cur += ch
        if cur:
            words.append(cur)
    return words


class WordPiece:
    def __init__(self, vocab_path=None):
        lines = open(vocab_path or config.MODELS / TEXT_FILES["vocab"], encoding="utf-8").read().split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        self.vocab = {t: i for i, t in enumerate(lines)}

    def word_ids(self, word: str) -> list[int]:
        if len(word) > 100:
            return [UNK]
        ids, start = [], 0
        while start < len(word):
            end, found = len(word), None
            while start < end:
                piece = word[start:end] if start == 0 else "##" + word[start:end]
                if piece in self.vocab:
                    found = self.vocab[piece]
                    break
                end -= 1
            if found is None:
                return [UNK]
            ids.append(found)
            start = end
        return ids

    def encode(self, text: str) -> list[int]:
        ids = [CLS]
        for w in basic_tokens(text):
            ids.extend(self.word_ids(w))
        return ids[:MAX_LEN - 1] + [SEP]


# ---------------------------------------------------------------- text encoder

class TextEncoder:
    _inst = None
    _lock = threading.Lock()

    def __init__(self):
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.intra_op_num_threads = 2
        self.sess = ort.InferenceSession(str(config.MODELS / TEXT_FILES["text"]), so, providers=["CPUExecutionProvider"])
        self.dense = np.fromfile(config.MODELS / TEXT_FILES["dense"], dtype="<f4").reshape(512, 768)
        self.tok = WordPiece()

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._inst is None:
                cls._inst = TextEncoder()
            return cls._inst

    def embed(self, text: str) -> np.ndarray:
        ids = np.array([self.tok.encode(text)], dtype=np.int64)
        h = self.sess.run(None, {"input_ids": ids, "attention_mask": np.ones_like(ids)})[0][0]
        v = h.mean(0) @ self.dense.T
        return (v / max(float(np.linalg.norm(v)), 1e-9)).astype(np.float32)


# ---------------------------------------------------------------- image vectors (all photos)

class ImageEncoder:
    def __init__(self):
        from .models import CLIP_DIMS, _session

        self.sess = _session("search_vision", {"pixel_values": (8, 3, 224, 224)}, CLIP_DIMS)

    def embed(self, imgs224):
        from .models import CLIP_MEAN, CLIP_STD

        out = []
        for i in range(0, len(imgs224), 8):
            chunk = imgs224[i:i + 8]
            x = np.zeros((8, 3, 224, 224), np.float32)
            x[:len(chunk)] = ((np.stack(chunk).astype(np.float32) / 255.0 - CLIP_MEAN) / CLIP_STD).transpose(0, 3, 1, 2)
            out.append(self.sess.run(["image_embeds"], {"pixel_values": x})[0][:len(chunk)])
        e = np.concatenate(out).astype(np.float32) if out else np.zeros((0, 512), np.float32)
        return e / np.maximum(np.linalg.norm(e, axis=1, keepdims=True), 1e-9)


def ready() -> bool:
    from .models import missing_models

    return not [m for m in missing_models() if m.startswith("search")]


def index_job(prog=None) -> int:
    """Add search vectors for photos that do not have one. Uses the thumbnails (no original is read again)."""
    from .models import clip_input, missing_models  # noqa: F401

    if "search_vision" in missing_models():
        return 0
    rows = db.q("SELECT p.id, p.thumb FROM photos p LEFT JOIN photo_vec v ON v.photo_id=p.id "
                "WHERE v.photo_id IS NULL AND p.thumb IS NOT NULL ORDER BY p.id")
    if not rows:
        return 0
    if prog:
        prog.set_phase("Making photos searchable", len(rows))
    enc = ImageEncoder()
    n = 0
    for i in range(0, len(rows), 32):
        chunk = rows[i:i + 32]
        imgs, ids = [], []
        for pid, rel in chunk:
            p = config.cache_dir("thumbs") / rel
            try:
                imgs.append(clip_input(Image.open(p).convert("RGB")))
                ids.append(pid)
            except OSError:
                continue
        t0 = time.perf_counter()
        E = enc.embed(imgs)
        if config.settings()["low_priority"]:
            time.sleep(min(1.0, time.perf_counter() - t0))
        with db.tx() as c:
            c.executemany("INSERT OR REPLACE INTO photo_vec(photo_id, emb) VALUES(?,?)",
                          [(pid, e.astype(np.float16).tobytes()) for pid, e in zip(ids, E)])
        n += len(ids)
        if prog:
            prog.step(len(chunk))
    _cache["ver"] = None
    return n


_cache = {"ver": None, "ids": None, "E": None}
_clock = threading.Lock()


def vectors(device_source: int | None = None):
    if device_source is not None:
        rows = db.q("""SELECT v.photo_id, v.emb FROM photo_vec v JOIN photos p ON p.id=v.photo_id
                       WHERE p.missing=0 AND EXISTS(SELECT 1 FROM files f WHERE f.photo_id=p.id AND f.source_id=?
                       AND f.status='done') ORDER BY v.photo_id""", (device_source,))
        return _arrays(rows)
    with _clock:
        ver = db.q1("SELECT COUNT(*), COALESCE(MAX(photo_id),0) FROM photo_vec")[:]
        if _cache["ver"] != ver:
            rows = db.q("SELECT v.photo_id, v.emb FROM photo_vec v JOIN photos p ON p.id=v.photo_id "
                        "WHERE p.missing=0 ORDER BY v.photo_id")
            _cache["ids"], _cache["E"] = _arrays(rows)
            _cache["ver"] = ver
        return _cache["ids"], _cache["E"]


def _arrays(rows):
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    E = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(-1, 512).astype(np.float32) if rows \
        else np.zeros((0, 512), np.float32)
    return ids, E


def rank(sims: np.ndarray, ids: np.ndarray, limit=300):
    """Best matches first. Keep results close to the best one (relative cut), at most `limit`."""
    if not len(sims):
        return []
    order = np.argsort(-sims)[:limit]
    best = float(sims[order[0]])
    cut = best - 0.10
    return [(int(ids[i]), round(float(sims[i]), 4)) for i in order if sims[i] >= cut]


def search(query: str, limit=300):
    q = query.strip()
    if not q:
        return []
    v = TextEncoder.get().embed(q)
    ids, E = vectors()
    return rank(E @ v, ids, limit)


def device_blob(device_source: int) -> bytes:
    """Binary file for the phone: b'RGV1', uint32 n, uint32 dim, n int64 photo ids, n*dim float16 (little-endian)."""
    import struct

    ids, E = vectors(device_source)
    return b"RGV1" + struct.pack("<II", len(ids), 512) + ids.astype("<i8").tobytes() + E.astype("<f2").tobytes()

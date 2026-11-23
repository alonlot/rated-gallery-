"""Image decoding (reduced size where possible), orientation, thumbnails, hashes."""
from __future__ import annotations

import io
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image, ImageFile, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pillow_heif = None

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 400_000_000

IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".3gp", ".mkv", ".avi", ".m4v", ".mts", ".m2ts", ".webm", ".mp", ".wmv", ".gif"}

ORIENT_SWAP = {5, 6, 7, 8}


def is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXT


def decode(data: bytes, max_side: int | None, min_side_hint: int | None = None):
    """Decode bytes to an oriented RGB PIL image whose longest side is <= max_side.

    JPEG uses draft mode (DCT scaling), so a 12 MP photo decodes at 1/2, 1/4 or 1/8 size.
    Returns (image, info) where info has the oriented full-size width/height, exif and format.
    """
    im = Image.open(io.BytesIO(data))
    fmt = im.format
    full_w, full_h = im.size
    try:
        exif = im.getexif()
    except Exception:  # noqa: BLE001  broken EXIF must not stop the scan
        exif = Image.Exif()
    orient = int(exif.get(274, 1) or 1)
    if max_side and fmt == "JPEG":
        scale = max_side / max(full_w, full_h)
        if scale < 1:
            im.draft("RGB", (max(1, int(full_w * scale)), max(1, int(full_h * scale))))
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    im = ImageOps.exif_transpose(im) if orient != 1 else im
    if orient in ORIENT_SWAP:
        full_w, full_h = full_h, full_w
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.BILINEAR, reducing_gap=2.0)
    return im, {"width": full_w, "height": full_h, "exif": exif, "format": fmt, "orientation": orient}


def decode_region(data: bytes, box_norm, out_side: int, margin: float):
    """Decode a square region around box_norm (x1,y1,x2,y2 in 0..1 of the oriented image).

    Uses the lowest JPEG draft scale that still gives the region at >= out_side pixels.
    Returns (crop RGB image, full image at decoded scale, info).
    """
    im = Image.open(io.BytesIO(data))
    full_w, full_h = im.size
    exif = im.getexif()
    orient = int(exif.get(274, 1) or 1)
    ow, oh = (full_h, full_w) if orient in ORIENT_SWAP else (full_w, full_h)
    x1, y1, x2, y2 = box_norm
    side_px = max((x2 - x1) * ow, (y2 - y1) * oh) * margin
    need = max(out_side / max(side_px, 1), 1600 / max(ow, oh))
    if im.format == "JPEG" and need < 1:
        im.draft("RGB", (max(1, int(full_w * need)), max(1, int(full_h * need))))
    im = im.convert("RGB")
    im = ImageOps.exif_transpose(im) if orient != 1 else im
    return square_crop(im, box_norm, margin, out_side), im, {"width": ow, "height": oh, "exif": exif}


def square_crop(im: Image.Image, box_norm, margin: float, out_side: int) -> Image.Image:
    W, H = im.size
    x1, y1, x2, y2 = box_norm
    cx, cy = (x1 + x2) / 2 * W, (y1 + y2) / 2 * H
    side = max((x2 - x1) * W, (y2 - y1) * H) * margin
    box = (int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2))
    crop = im.crop(box)  # PIL pads outside areas with black
    return crop.resize((out_side, out_side), Image.Resampling.BICUBIC)


def letterbox(arr: np.ndarray, size: int = 640):
    """Resize keeping aspect ratio into a size x size canvas (top-left). Returns (canvas, scale)."""
    import cv2

    h, w = arr.shape[:2]
    scale = size / max(h, w)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    canvas[:nh, :nw] = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    return canvas, scale


def phash_hex(im: Image.Image) -> str:
    return str(imagehash.phash(im))


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def save_webp(im: Image.Image, path: Path, max_side: int, quality: int = 72):
    path.parent.mkdir(parents=True, exist_ok=True)
    t = im.copy()
    t.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    tmp = path.with_suffix(".tmp")
    t.save(tmp, "WEBP", quality=quality, method=4)
    tmp.replace(path)


def save_jpeg(im: Image.Image, path: Path, max_side: int | None = None, quality: int = 85):
    path.parent.mkdir(parents=True, exist_ok=True)
    t = im
    if max_side and max(im.size) > max_side:
        t = im.copy()
        t.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    tmp = path.with_suffix(".tmp")
    t.save(tmp, "JPEG", quality=quality)
    tmp.replace(path)
    return path.stat().st_size


def jpeg_bytes(im: Image.Image, max_side: int | None = None, quality: int = 85) -> bytes:
    t = im
    if max_side and max(im.size) > max_side:
        t = im.copy()
        t.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    b = io.BytesIO()
    t.save(b, "JPEG", quality=quality)
    return b.getvalue()

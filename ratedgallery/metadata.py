"""Metadata: Google Takeout sidecar first, then EXIF.

Takeout sidecar names are irregular:
  IMG_1.JPG.json, IMG_1.JPG.supplemental-metadata.json, truncated forms (".supplemental-metad.json"),
  duplicates "IMG_1(1).JPG" -> "IMG_1.JPG(1).json", edited copies "IMG_1-edited.JPG" -> "IMG_1.JPG.json".
The sidecar "title" field holds the original file name, so the index uses it as the key.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

EDIT_SUFFIXES = ("-edited", "-bearbeitet", "-modifié", "-modificado", "-modificato", "-bewerkt",
                 "-edytowane", "-redigeret", "-muokattu", "-editado", "-ערוך")
DUP_RE = re.compile(r"^(.*)\((\d+)\)$")
JSON_DUP_RE = re.compile(r"\((\d+)\)\.json$", re.I)
# camera file names with the capture time: IMG_20230310_150000.jpg, PXL_20240707_190000123.jpg, 20230310_150000.jpg,
# Screenshot_20230310-150000.png, signal-2023-03-10-150000.jpg is not matched (dashes in the date)
NAME_TIME_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_\-T. ]?(\d{2})(\d{2})(\d{2})\d{0,3}(?!\d)")


def time_from_name(name: str | None) -> datetime | None:
    """Local capture time from a camera-style file name, or None."""
    m = NAME_TIME_RE.search(Path(name or "").stem)
    if not m:
        return None
    y, mo, d, hh, mm, ss = map(int, m.groups())
    if not 1990 <= y <= datetime.now().year + 1:
        return None
    try:
        return datetime(y, mo, d, hh, mm, ss)
    except ValueError:
        return None


# ---------------------------------------------------------------- Takeout sidecars

def index_sidecars(json_paths):
    """Return {lower(title): [(dupnum, path)]} for the sidecar files of one directory."""
    idx: dict[str, list] = {}
    for p in json_paths:
        try:
            if p.stat().st_size > 1_000_000:
                continue
            d = json.loads(Path(p).read_text("utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or "title" not in d or ("photoTakenTime" not in d and "geoData" not in d):
            continue
        m = JSON_DUP_RE.search(Path(p).name)
        n = int(m.group(1)) if m else 0
        idx.setdefault(str(d["title"]).lower(), []).append((n, str(p)))
    return idx


def media_key(name: str):
    """'IMG_1-edited(2).JPG' -> ('img_1.jpg', 2)"""
    stem, ext = Path(name).stem, Path(name).suffix
    n = 0
    m = DUP_RE.match(stem)
    if m:
        stem, n = m.group(1), int(m.group(2))
    low = stem.lower()
    for s in EDIT_SUFFIXES:
        if low.endswith(s):
            stem = stem[: -len(s)]
            break
    return (stem + ext).lower(), n


def match_sidecar(name: str, idx: dict) -> str | None:
    base, n = media_key(name)
    cands = idx.get(base)
    if not cands:
        # Takeout truncates long names: the title is complete, the file name is not
        stem = Path(base).stem
        if len(stem) >= 20:
            for title, lst in idx.items():
                if title.startswith(stem):
                    cands = lst
                    break
    if not cands:
        return None
    for cn, p in cands:
        if cn == n:
            return p
    return cands[0][1]


def read_sidecar(path: str | None) -> dict:
    if not path:
        return {}
    try:
        d = json.loads(Path(path).read_text("utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    out = {}
    try:
        ts = d.get("photoTakenTime", {}).get("timestamp")
        if ts and int(ts) > 0:
            out["taken_ts"] = float(ts)
    except (TypeError, ValueError, AttributeError):
        pass
    for key in ("geoData", "geoDataExif"):
        g = d.get(key) or {}
        try:
            lat, lon = float(g.get("latitude", 0)), float(g.get("longitude", 0))
        except (TypeError, ValueError):
            continue
        if abs(lat) > 1e-6 or abs(lon) > 1e-6:
            out["lat"], out["lon"] = lat, lon
            break
    return out


# ---------------------------------------------------------------- EXIF

def _ratio(v):
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        try:
            return float(v[0]) / float(v[1])
        except Exception:  # noqa: BLE001
            return None


def _dms(v, ref):
    try:
        d, m, s = (_ratio(x) for x in v)
        r = d + m / 60 + s / 3600
        if ref in ("S", "W", b"S", b"W"):
            r = -r
        return r
    except Exception:  # noqa: BLE001
        return None


def _str(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    v = str(v).strip().strip("\x00").strip()
    return v or None


def parse_exif(exif) -> dict:
    out = {}
    if exif is None:
        return out
    try:
        base = dict(exif)
        sub = dict(exif.get_ifd(0x8769)) if hasattr(exif, "get_ifd") else {}
        gps = dict(exif.get_ifd(0x8825)) if hasattr(exif, "get_ifd") else {}
    except Exception:  # noqa: BLE001
        return out
    out["make"] = _str(base.get(271))
    out["model"] = _str(base.get(272))
    out["orientation"] = int(base.get(274, 1) or 1)
    dto = _str(sub.get(36867)) or _str(sub.get(36868)) or _str(base.get(306))
    if dto:
        try:
            out["exif_local"] = datetime.strptime(dto[:19], "%Y:%m:%d %H:%M:%S")
            sub_s = _str(sub.get(37521))
            if sub_s and sub_s.isdigit():
                out["exif_local"] = out["exif_local"].replace(microsecond=int(float("0." + sub_s) * 1e6))
        except ValueError:
            pass
    off = _str(sub.get(36881)) or _str(sub.get(36880))
    if off and re.match(r"^[+-]\d\d:\d\d$", off):
        out["exif_offset"] = off
    out["focal"] = _ratio(sub.get(37386)) if sub.get(37386) is not None else None
    f35 = sub.get(41989)
    out["focal35"] = float(f35) if f35 else None
    out["lens"] = _str(sub.get(42036)) or _str(sub.get(42035))
    if gps.get(2) and gps.get(4):
        lat, lon = _dms(gps[2], gps.get(1)), _dms(gps[4], gps.get(3))
        if lat is not None and lon is not None and (abs(lat) > 1e-6 or abs(lon) > 1e-6) \
                and -90 <= lat <= 90 and -180 <= lon <= 180:
            out["lat"], out["lon"] = lat, lon
    return out


def facing_from_lens(lens: str | None) -> str:
    if not lens:
        return "unknown"
    s = lens.lower()
    if "front" in s or "selfie" in s or "truedepth" in s:
        return "front"
    if "back" in s or "rear" in s or "main" in s or "wide" in s or "telephoto" in s:
        return "rear"
    return "unknown"


def merge(exif_info: dict, sidecar: dict, taken_hint: float | None = None, name: str | None = None) -> dict:
    """Combine. Sidecar first for date (UTC moment) and GPS; EXIF gives local time and camera.
    Without any of these, the capture time comes from a camera-style file name (IMG_20230310_150000.jpg).
    Without a capture time a copy could be matched with the wrong photo (see pipeline._handle_scan)."""
    m = {k: exif_info.get(k) for k in ("make", "model", "lens", "focal", "focal35", "orientation")}
    local = exif_info.get("exif_local")
    ts = sidecar.get("taken_ts")
    if ts is None and local is not None and exif_info.get("exif_offset"):
        sign = 1 if exif_info["exif_offset"][0] == "+" else -1
        hh, mm = exif_info["exif_offset"][1:].split(":")
        ts = local.replace(tzinfo=timezone.utc).timestamp() - sign * (int(hh) * 3600 + int(mm) * 60)
    if ts is None and taken_hint:
        ts = float(taken_hint)  # phone: MediaStore DATE_TAKEN (UTC)
    if ts is None and local is None:
        local = time_from_name(name)
    if ts is None and local is not None:
        ts = time.mktime(local.timetuple()) + local.microsecond / 1e6  # PC time zone: best guess
    if local is None and ts is not None:
        local = datetime.fromtimestamp(ts)  # PC time zone; best guess without EXIF
    m["taken_ts"] = ts
    m["taken_local"] = local.strftime("%Y-%m-%dT%H:%M:%S") if local else None
    if sidecar.get("lat") is not None:
        m["lat"], m["lon"] = sidecar["lat"], sidecar["lon"]
    else:
        m["lat"], m["lon"] = exif_info.get("lat"), exif_info.get("lon")
    m["facing"] = facing_from_lens(m.get("lens"))
    return m

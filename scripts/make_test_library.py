"""Make a synthetic test library from test_data/base (public-domain NASA portraits).

Output: test_data/generated/
  takeout/Takeout/Google Photos/...   (JPG, HEIC, PNG, sidecars with name variants, duplicates, bursts, a video)
  phone/DCIM/Camera/...               (higher-resolution copies of some Takeout photos + phone-only photos)
  truth.json                          {file name: true quality 1..10} for photos of "me" (person A)
"""
import json
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "test_data" / "base"
OUT = ROOT / "test_data" / "generated"

PLACES = {"rome": (41.8902, 12.4922), "telaviv": (32.0853, 34.7818), "paris": (48.8566, 2.3522)}

VARIANTS = {  # name: (function, quality delta from the base quality)
    "orig": (lambda im: im, 0.0),
    "zoom": (lambda im: im.crop((im.width * .1, im.height * .05, im.width * .9, im.height * .85)).resize(im.size), 0.0),
    "rot": (lambda im: im.rotate(12, resample=Image.Resampling.BICUBIC, fillcolor=(40, 40, 40)), -1.5),
    "flat": (lambda im: ImageEnhance.Contrast(im).enhance(0.45), -2.5),
    "bright": (lambda im: ImageEnhance.Brightness(im).enhance(1.9), -3.5),
    "dark": (lambda im: ImageEnhance.Brightness(im).enhance(0.35), -4.0),
    "small": (lambda im: _small(im), -3.0),
    "blur2": (lambda im: im.filter(ImageFilter.GaussianBlur(2.5)), -3.5),
    "blur5": (lambda im: im.filter(ImageFilter.GaussianBlur(6)), -5.5),
}
BASE_Q = {"me_0": 8.5, "me_1": 9.0, "me_2": 8.0, "me_3": 8.0, "me_5": 7.0}


def _small(im):
    canvas = Image.new("RGB", (im.width * 2, im.height * 2), (90, 110, 130))
    canvas.paste(im, (im.width // 2, im.height // 2))
    return canvas.resize(im.size)


def exif_bytes(dt=None, gps=None, lens=None, model="Pixel 7", orientation=None):
    ex = Image.Exif()
    ex[271] = "Google"
    ex[272] = model
    if orientation:
        ex[274] = orientation
    sub = {}
    if dt:
        sub[36867] = dt.strftime("%Y:%m:%d %H:%M:%S")
    if lens:
        sub[42036] = lens
    sub[37386] = 2.2 if lens and "front" in lens else 6.8
    if sub:
        ex[0x8769] = sub
    if gps:
        lat, lon = gps

        def dms(v):
            v = abs(v)
            d = int(v); m = int((v - d) * 60); s = round(((v - d) * 60 - m) * 60, 2)
            return (float(d), float(m), float(s))
        ex[0x8825] = {1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W", 4: dms(lon)}
    return ex.tobytes()


def sidecar(path: Path, title: str, ts: float | None, gps=None, name=None):
    d = {"title": title, "description": "", "imageViews": "0",
         "creationTime": {"timestamp": str(int(ts or 0)), "formatted": ""},
         "photoTakenTime": {"timestamp": str(int(ts)) if ts else "0", "formatted": ""},
         "geoData": {"latitude": gps[0] if gps else 0.0, "longitude": gps[1] if gps else 0.0, "altitude": 0.0},
         "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}}
    (path.parent / (name or (path.name + ".json"))).write_text(json.dumps(d))


def main():
    if not any(BASE.glob("me_*.jpg")):
        sys.exit("Run scripts/fetch_test_faces.py first")
    if OUT.exists():
        shutil.rmtree(OUT)
    tk = OUT / "takeout" / "Takeout" / "Google Photos"
    y23, y24, album = tk / "Photos from 2023", tk / "Photos from 2024", tk / "Trip to Italy"
    phone = OUT / "phone" / "DCIM" / "Camera"
    for d in (y23, y24, album, phone):
        d.mkdir(parents=True)
    (album / "metadata.json").write_text(json.dumps({"title": "Trip to Italy", "description": ""}))
    rng = random.Random(7)
    truth = {}
    t0 = datetime(2023, 3, 1, 10, 0, tzinfo=timezone.utc)
    k = 0

    def when():
        nonlocal k
        k += 1
        return t0 + timedelta(days=k * 9, hours=rng.randint(0, 10))

    # photos of me with known quality
    for base, bq in BASE_Q.items():
        src = Image.open(BASE / f"{base}.jpg").convert("RGB")
        for vname, (fn, dq) in VARIANTS.items():
            im = fn(src)
            dt = when()
            place = rng.choice([None, "rome", "telaviv", "paris"])
            front = rng.random() < 0.35
            name = f"IMG_{dt:%Y%m%d_%H%M%S}_{base}_{vname}.jpg"
            folder = y23 if dt.year == 2023 else y24
            q = max(1.0, min(10.0, bq + dq + (-1.0 if front else 0.0) * 0))
            truth[name] = round(q, 2)
            im.save(folder / name, "JPEG", quality=90, exif=exif_bytes(
                dt.replace(tzinfo=None), None, "Pixel 7 front camera 2.2mm f/2.2" if front else "Pixel 7 back camera 6.81mm f/1.85"))
            # sidecar holds GPS (Google removed it from the file); some use the new suffix
            if rng.random() < 0.5:
                sidecar(folder / name, name, dt.timestamp(), PLACES.get(place))
            else:
                sidecar(folder / name, name, dt.timestamp(), PLACES.get(place), name + ".supplemental-metadata.json")

    # other people (filters only)
    for base in ("dana_0", "dana_1", "dana_2", "other1_0", "other1_1", "other2_0", "other2_1", "group_0", "group_1", "me_4"):
        src = Image.open(BASE / f"{base}.jpg").convert("RGB")
        for vname in (("orig", "dark") if base.startswith(("dana_0", "other")) else ("orig",)):
            im = VARIANTS[vname][0](src)
            dt = when()
            name = f"PXL_{dt:%Y%m%d_%H%M%S}_{base}_{vname}.jpg"
            im.save(y24 / name, "JPEG", quality=90, exif=exif_bytes(dt.replace(tzinfo=None), PLACES["rome"] if "dana" in base else None))
            sidecar(y24 / name, name, dt.timestamp(), PLACES["rome"] if base == "me_4" else None)
            if base == "me_4":
                truth[name] = 6.0

    # me together with Dana (two people in one photo)
    a = Image.open(BASE / "me_0.jpg").convert("RGB")
    b = Image.open(BASE / "dana_0.jpg").convert("RGB")
    hgt = 900
    a = a.resize((int(a.width * hgt / a.height), hgt)); b = b.resize((int(b.width * hgt / b.height), hgt))
    both = Image.new("RGB", (a.width + b.width, hgt)); both.paste(a, (0, 0)); both.paste(b, (a.width, 0))
    dt = datetime(2024, 7, 7, 19, 0)
    name = "PXL_20240707_190000_me_with_dana.jpg"
    both.save(y24 / name, "JPEG", quality=90, exif=exif_bytes(dt, PLACES["rome"], "Pixel 7 back camera 6.81mm f/1.85"))
    truth[name] = 7.5

    # duplicates and bursts
    first = sorted(y23.glob("*me_0_orig.jpg"))[0]
    shutil.copy2(first, album / first.name)                                     # exact duplicate in an album
    shutil.copy2(first.with_name(first.name + ".json") if first.with_name(first.name + ".json").exists()
                 else first.with_name(first.name + ".supplemental-metadata.json"), album / (first.name + ".json"))
    im = Image.open(first)
    im.resize((im.width // 2, im.height // 2)).save(album / first.name.replace(".jpg", "(1).jpg"), quality=80)  # near dup, lower res
    sidecar(album / first.name, first.name, None, PLACES["rome"], first.name + "(1).json")
    src = Image.open(BASE / "me_1.jpg").convert("RGB")
    bt = datetime(2024, 6, 1, 12, 0, 0)
    for i in range(3):                                                           # burst: 3 shots 1 s apart
        shot = src.crop((4 * i, 3 * i, src.width - 6 + 4 * i, src.height - 6 + 3 * i)).resize(src.size)
        if i == 2:
            shot = shot.filter(ImageFilter.GaussianBlur(1.2))
        name = f"IMG_20240601_12000{i}_BURST.jpg"
        shot.save(y24 / name, "JPEG", quality=90, exif=exif_bytes(bt + timedelta(seconds=i), PLACES["paris"]))
        truth[name] = 8.5 if i < 2 else 6.0
    # edited copy uses the original's sidecar
    e = sorted(y24.glob("*me_2_zoom.jpg"))
    if e:
        ImageEnhance.Color(Image.open(e[0])).enhance(1.3).save(y24 / e[0].name.replace(".jpg", "-edited.jpg"), quality=90)
        truth[e[0].name.replace(".jpg", "-edited.jpg")] = truth[e[0].name]

    # other formats
    import pillow_heif
    pillow_heif.register_heif_opener()
    h = Image.open(BASE / "me_3.jpg").convert("RGB")
    h.save(y24 / "IMG_HEIC_me_3.HEIC", exif=exif_bytes(datetime(2024, 8, 8, 18, 30), PLACES["telaviv"]))
    truth["IMG_HEIC_me_3.HEIC"] = 8.0
    Image.open(BASE / "me_2.jpg").convert("RGB").save(y24 / "Screenshot_me_2.png")
    truth["Screenshot_me_2.png"] = 8.0
    rot = Image.open(BASE / "me_0.jpg").convert("RGB").rotate(90, expand=True)  # pixels rotated, EXIF says rotate back
    rot.save(y24 / "IMG_ROTATED_me_0.jpg", quality=90, exif=exif_bytes(datetime(2024, 9, 9, 9, 9), None, orientation=6))
    truth["IMG_ROTATED_me_0.jpg"] = 8.5
    (y24 / "VID_20240101_000000.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1000)
    long_name = "IMG_20240505_101010_with_a_very_long_file_name_me_1.jpg"           # Takeout truncates sidecar names
    Image.open(BASE / "me_1.jpg").convert("RGB").save(y24 / long_name, quality=90)
    truth[long_name] = 9.0
    sidecar(y24 / long_name, long_name, datetime(2024, 5, 5, 10, 10, 10, tzinfo=timezone.utc).timestamp(),
            PLACES["paris"], long_name[:46] + ".json")

    # phone: higher resolution copies of a few Takeout photos + phone-only photos
    for p in sorted(y23.glob("*_orig.jpg"))[:3]:
        im = Image.open(p)
        im.resize((im.width * 2, im.height * 2), Image.Resampling.LANCZOS).save(phone / p.name, quality=95, exif=im.info.get("exif", b""))
    src = Image.open(BASE / "me_2.jpg").convert("RGB")
    for i, (vname, (fn, dq)) in enumerate(list(VARIANTS.items())[:4]):
        name = f"IMG_PHONE_ONLY_{i}_{vname}.jpg"
        fn(src).save(phone / name, quality=92, exif=exif_bytes(datetime(2025, 1, 10 + i, 15, 0), PLACES["telaviv"]))
        truth[name] = max(1.0, BASE_Q["me_2"] + dq)
    Image.open(BASE / "dana_0.jpg").convert("RGB").save(phone / "IMG_PHONE_dana.jpg", quality=92)

    (OUT / "truth.json").write_text(json.dumps(truth, indent=1))
    n_tk = sum(1 for _ in (OUT / "takeout").rglob("*") if _.suffix.lower() in (".jpg", ".png", ".heic"))
    print(f"takeout images: {n_tk}, phone images: {len(list(phone.iterdir()))}, truth: {len(truth)}")


if __name__ == "__main__":
    main()

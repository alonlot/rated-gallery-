"""Download public-domain NASA portraits into test_data/base (one time, for tests only).

NASA images are generally not subject to copyright in the United States
(https://www.nasa.gov/nasa-brand-center/images-and-media/). The people in them are public figures;
the tests use them only as anonymous identities A, B, C, D.
"""
import sys
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "test_data" / "base"

IMAGES = {
    "me": ["NHQ201907080003", "jsc2010e124007", "S99-05705", "S96-14778", "jsc2015e032666", "201503200002HQ"],
    "dana": ["JSC2001-03044", "iss050-s-002", "iss016-s-002b"],
    "other1": ["s97-00690", "jsc2012e096295"],
    "other2": ["iss072e189112", "jsc2018e067953"],
    "group": ["iss043-s-002", "iss016-s-002c"],
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    c = httpx.Client(timeout=60, follow_redirects=True)
    for person, ids in IMAGES.items():
        for i, nid in enumerate(ids):
            dst = OUT / f"{person}_{i}.jpg"
            if dst.exists():
                continue
            for size in ("large", "medium", "orig"):
                r = c.get(f"https://images-assets.nasa.gov/image/{nid}/{nid}~{size}.jpg")
                if r.status_code == 200:
                    tmp = dst.with_suffix(".tmp")
                    tmp.write_bytes(r.content)
                    im = Image.open(tmp).convert("RGB")
                    im.thumbnail((1200, 1200))
                    im.save(dst, "JPEG", quality=90)
                    tmp.unlink()
                    print("ok", dst.name, nid, size)
                    break
            else:
                print("FAILED", nid, file=sys.stderr)


if __name__ == "__main__":
    main()

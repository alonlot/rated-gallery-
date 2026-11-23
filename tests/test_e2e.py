"""End-to-end test on the synthetic library (CPU). Tests run in order and share one database."""
import hashlib
import json
import time
from pathlib import Path

import pytest

from conftest import need_models

LAN = ("192.168.1.50", 50000)


def wait_idle(timeout=600):
    from ratedgallery.tasks import runner

    t = time.time()
    time.sleep(0.3)
    while runner.busy():
        assert time.time() - t < timeout, "task did not finish"
        time.sleep(0.3)
    hist = runner.status()["history"]
    assert not [h for h in hist if h["status"] == "error"], hist


def tree_hash(root: Path):
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()


@pytest.fixture(scope="module")
def env(library, data_dir):
    need_models()
    from fastapi.testclient import TestClient

    from ratedgallery import db, server

    db.kv_set("benchmark", {"skipped": "tests"})
    truth = json.loads((library / "truth.json").read_text())
    client = TestClient(server.app)
    client.__enter__()
    yield {"lib": library, "truth": truth, "client": client, "data": data_dir,
           "before": tree_hash(library / "takeout"), "before_phone": tree_hash(library / "phone")}
    client.__exit__(None, None, None)


def name_of_photo():
    from ratedgallery import db

    return {r[0]: Path(r[1]).name for r in db.q("SELECT p.id, f.rel FROM photos p JOIN files f ON f.id=p.best_file")}


def test_scan_takeout(env):
    from ratedgallery import db

    c = env["client"]
    r = c.post("/api/sources", json={"path": str(env["lib"] / "takeout")})
    assert r.status_code == 200
    assert c.post("/api/scan").status_code == 200
    wait_idle()
    st = c.get("/api/status").json()
    assert st["stats"]["files"].get("error", 0) == 0
    assert st["stats"]["files"]["done"] == 70            # the .mp4 is skipped, not a file
    rows = {Path(r[0]).name: r for r in db.q("SELECT f.rel, f.photo_id, p.* FROM files f JOIN photos p ON p.id=f.photo_id")}
    orig = next(k for k in rows if k.endswith("me_0_orig.jpg"))
    copies = [k for k in rows if "me_0_orig" in k]
    assert len({rows[k][1] for k in copies}) == 1, "exact and resized copies must be one photo"
    assert rows[orig]["width"] == 1200                    # the full-size copy is kept
    bursts = {rows[k]["burst"] for k in rows if "BURST" in k}
    assert len(bursts) == 1 and None not in bursts
    assert len({rows[k][1] for k in rows if "BURST" in k}) == 3   # bursts are kept, not removed
    heic = rows["IMG_HEIC_me_3.HEIC"]
    assert heic["n_faces"] >= 1 and heic["city"] == "Tel Aviv"
    rot = rows["IMG_ROTATED_me_0.jpg"]
    assert rot["width"] < rot["height"] or rot["width"] == 1200  # oriented size
    longn = rows["IMG_20240505_101010_with_a_very_long_file_name_me_1.jpg"]
    assert longn["country"] == "France"                   # GPS came from the truncated sidecar
    rome = [r for k, r in rows.items() if r["cc"] == "IT"]
    assert rome and all(r["country"] == "Italy" for r in rome)


def test_rescan_is_incremental(env):
    from ratedgallery import db

    n = db.val("SELECT COUNT(*) FROM photos")
    env["client"].post("/api/scan")
    wait_idle()
    assert db.val("SELECT COUNT(*) FROM photos") == n


def test_find_me_and_people(env):
    from ratedgallery import db

    c = env["client"]
    cl = c.get("/api/clusters").json()
    assert cl["clusters"], "faces must be grouped"
    names = name_of_photo()
    refs = []
    for pid, n in names.items():
        if ("_orig" in n or "_zoom" in n) and "me_" in n and "group" not in n:
            fid = db.val("SELECT id FROM faces WHERE photo_id=? ORDER BY size_px DESC LIMIT 1", (pid,))
            if fid:
                refs.append(fid)
    r = c.post("/api/persons", json={"name": "Me", "face_ids": refs[:7], "is_me": True})
    assert r.status_code == 200
    dana = db.val("SELECT f.id FROM faces f JOIN photos p ON p.id=f.photo_id JOIN files x ON x.id=p.best_file "
                  "WHERE x.rel LIKE '%dana_0_orig%' ORDER BY f.size_px DESC LIMIT 1")
    did = c.post("/api/persons", json={"name": "Dana", "face_ids": [dana]}).json()["id"]
    wait_idle()
    me_photos = {r[0] for r in db.q("SELECT id FROM photos WHERE me_face IS NOT NULL")}
    truth = env["truth"]
    expected = {pid for pid, n in names.items() if n in truth}
    recall = len(expected & me_photos) / len(expected)
    assert recall >= 0.9, recall
    # confirm / reject flow
    me = c.get("/api/people").json()["me"]
    unc = c.get(f"/api/persons/{me}/uncertain").json()["faces"]
    if unc:
        assert c.post(f"/api/persons/{me}/faces", json={"action": "reject", "face_ids": [unc[0]["id"]]}).status_code == 200
    # Dana appears in several photos (filter only, never scored)
    dana_photos = db.val("SELECT COUNT(DISTINCT photo_id) FROM faces WHERE person_id=? AND assign IN ('ref','confirmed','auto')", (did,))
    assert dana_photos >= 2
    assert db.val("SELECT COUNT(*) FROM scores s JOIN faces f ON f.id=s.face_id WHERE f.person_id=?", (did,)) == 0
    # faces of named people do not show in "unnamed groups"
    for cl in c.get("/api/clusters").json()["clusters"]:
        for fid in c.get(f"/api/clusters/{cl['id']}").json()["faces"]:
            assert db.val("SELECT assign FROM faces WHERE id=?", (fid,)) not in ("ref", "confirmed", "auto")


def test_features_for_my_photos(env):
    from ratedgallery import db

    wait_idle()
    n_me = db.val("SELECT COUNT(*) FROM photos WHERE me_face IS NOT NULL")
    n_feat = db.val("SELECT COUNT(*) FROM photos WHERE me_face IS NOT NULL AND stage2=1")
    assert n_feat == n_me
    feats = [json.loads(r[0]) for r in db.q("SELECT data FROM features")]
    assert sum(f["mp_ok"] for f in feats) / len(feats) > 0.7, "MediaPipe must find most faces"
    assert all(1.0 <= f["beauty"] <= 5.0 for f in feats), "attractiveness model output must be on its 1..5 scale"
    # photos without me get no features (expensive models run only on my photos)
    assert db.val("SELECT COUNT(*) FROM features f JOIN photos p ON p.id=f.photo_id WHERE p.me_face IS NULL") == 0
    # blurred photo is less sharp than the original
    names = name_of_photo()
    sharp = {names[r[0]]: json.loads(r[1])["sharp"] for r in db.q("SELECT photo_id, data FROM features")}
    o = next(v for k, v in sharp.items() if "me_1_orig" in k)
    b = next(v for k, v in sharp.items() if "me_1_blur5" in k)
    assert b < o
    assert db.val("SELECT COUNT(*) FROM scores") == n_me    # prior scores exist before any rating


def test_calibration_and_training(env):
    from ratedgallery import config, db

    c = env["client"]
    truth = env["truth"]
    names = name_of_photo()
    config.settings().update(retrain_every=10)
    seen = 0
    for _ in range(80):
        r = c.get("/api/calib/next").json()
        if r["kind"] == "done":
            break
        if r["kind"] == "pair":
            a, b = r["a"], r["b"]
            ta, tb = truth.get(names[a["photo_id"]], 5), truth.get(names[b["photo_id"]], 5)
            res = "a" if ta > tb + 0.5 else "b" if tb > ta + 0.5 else "eq"
            assert c.post("/api/calib/compare", json={"a": a["face_id"], "b": b["face_id"], "result": res}).status_code == 200
            continue
        it = r["item"]
        q = truth.get(names[it["photo_id"]])
        if q is None:
            c.post("/api/calib/skip", json={"face_id": it["face_id"]})
            continue
        assert c.post("/api/calib/rate", json={"face_id": it["face_id"], "rating": int(round(q))}).status_code == 200
        seen += 1
        if seen == 3:  # undo works and returns the same item
            u = c.post("/api/calib/undo").json()
            assert u["ok"] and u["redo"]["item"]["face_id"] == it["face_id"]
            c.post("/api/calib/rate", json={"face_id": it["face_id"], "rating": int(round(q))})
        if seen >= 30:
            break
    wait_idle()
    m = c.get("/api/model").json()
    assert m["ratings"] >= 25 and m["trained"]
    assert m["spearman"] is not None and m["spearman"] > 0.5, m
    b = m["beauty"]  # the attractiveness model is compared with and without, on the same folds
    assert b["with"] is not None and b["without"] is not None and isinstance(b["used"], bool) and b["message"]
    from ratedgallery import ranking
    _, model = ranking.current_model()
    assert (model["w"][ranking.BEAUTY_COL] != 0) == b["used"]
    # the trained model ranks unrated photos well too
    from scipy.stats import spearmanr

    rated = {r[0] for r in db.q("SELECT face_id FROM ratings WHERE active=1")}
    rows = [(names[r[1]], r[2]) for r in db.q("SELECT face_id, photo_id, score FROM scores") if r[0] not in rated]
    rows = [(truth[n], s) for n, s in rows if n in truth]
    assert len(rows) >= 8
    assert spearmanr([a for a, _ in rows], [b for _, b in rows]).statistic > 0.5
    # confidence: a heavily blurred face is less confident than the original
    conf = {names[r[0]]: r[1] for r in db.q("SELECT photo_id, conf FROM scores")}
    checked = 0
    for base in ("me_0", "me_1", "me_2", "me_3", "me_5"):
        blur = [v for k, v in conf.items() if f"{base}_blur5" in k]
        orig = [v for k, v in conf.items() if f"{base}_orig" in k]
        if blur and orig:  # a rejected uncertain face is not scored (correct), so some pairs can be absent
            assert blur[0] < orig[0], base
            checked += 1
    assert checked >= 2
    sur = c.get("/api/calib/surprising").json()["items"]
    assert isinstance(sur, list)


def test_text_search_english_and_hebrew(env):
    from ratedgallery import db

    c = env["client"]
    assert db.val("SELECT COUNT(*) FROM photo_vec") == db.val("SELECT COUNT(*) FROM photos")
    names = name_of_photo()
    orange = ("me_2", "dana_0", "other1_0")  # the portraits in orange space suits
    groups = ("group_", "dana_1", "dana_2", "me_4", "me_with_dana", "other2_1")  # photos with 2 or more people
    for q, want in (("orange space suit", orange), ("a group of astronauts", groups), ("קבוצה של אסטרונאוטים", groups)):
        r = c.get("/api/search", params={"q": q}).json()
        top = [names[i] for i, _ in r["results"][:5]]
        assert sum(any(o in n for o in want) for n in top) >= 3, (q, top)
        sims = [s for _, s in r["results"]]
        assert sims == sorted(sims, reverse=True) and sims[0] - sims[-1] <= 0.10 + 1e-6
    assert c.get("/api/search", params={"q": ""}).json()["results"] == []


def test_gallery_api(env, tmp_path):
    c = env["client"]
    idx = c.get("/api/index").json()
    me = [p for p in idx["photos"] if p["me"]]
    assert me and all(p["s"] is not None for p in me)
    assert any(p["lat"] is None for p in idx["photos"]) and any(p["t"] for p in idx["photos"])
    p = me[0]
    d = c.get(f"/api/photo/{p['id']}").json()
    assert d["breakdown"] and d["meta"]["Date"]
    assert c.get(f"/api/photo/{p['id']}/thumb").status_code == 200
    v = c.get(f"/api/photo/{p['id']}/view")
    assert v.status_code == 200 and v.headers["content-type"] == "image/jpeg"
    assert c.get(f"/api/face/{d['me_face']}/large").status_code == 200
    out = tmp_path / "export"
    assert c.post("/api/export", json={"photo_ids": [x["id"] for x in me[:3]], "folder": str(out)}).status_code == 200
    wait_idle()
    assert len(list(out.iterdir())) == 3
    r = c.post("/api/calib/rate", json={"face_id": d["me_face"], "rating": 7, "source": "gallery"})
    assert r.status_code == 200


def test_lan_access_and_phone_sync(env):
    from fastapi.testclient import TestClient

    from ratedgallery import db, pipeline, server

    c = env["client"]
    lan = TestClient(server.app, client=LAN)
    assert lan.get("/api/index").status_code == 403                     # LAN access off
    assert c.post("/api/settings/lan", json={"enabled": True}).status_code == 400   # needs a password
    assert c.post("/api/settings/lan", json={"enabled": True, "password": "secret123"}).status_code == 200
    assert lan.get("/api/index").status_code == 401                     # login required
    assert lan.post("/api/login", json={"password": "wrong"}).status_code == 403
    assert lan.post("/api/settings/lan", json={"enabled": False}).status_code == 401   # not from LAN
    # pairing
    code = c.post("/api/pair/start").json()["code"]
    assert lan.post("/api/device/pair", json={"code": "000000" if code != "000000" else "111111"}).status_code == 403
    tok = lan.post("/api/device/pair", json={"code": code, "name": "Test phone"}).json()["token"]
    assert lan.post("/api/device/pair", json={"code": code}).status_code == 403    # one use only
    H = {"Authorization": f"Bearer {tok}"}
    assert lan.get("/api/device/ping", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert lan.get("/api/device/ping", headers=H).json()["ok"]
    files = sorted((env["lib"] / "phone").rglob("*.jpg"))
    items = [{"mid": str(i + 1), "name": f.name, "size": f.stat().st_size, "mtime": int(f.stat().st_mtime),
              "taken": None, "mime": "image/jpeg", "path": "DCIM/Camera/"} for i, f in enumerate(files)]
    r = lan.post("/api/device/manifest", json={"full": True, "items": items}, headers=H).json()
    assert r["new"] == len(files)
    by_mid = {it["mid"]: f for it, f in zip(items, files)}
    sent = 0
    for _ in range(10):
        w = lan.get("/api/device/wanted?limit=5", headers=H).json()
        if not w["items"]:
            break
        for mid in w["items"]:
            assert lan.put(f"/api/device/file/{mid}", content=by_mid[mid].read_bytes(), headers=H).json()["ok"]
            sent += 1
    assert sent == len(files)
    wait_idle()
    assert lan.get("/api/device/status", headers=H).json()["pending"] == 0
    sid = db.val("SELECT source_id FROM devices")
    assert db.val("SELECT COUNT(*) FROM files WHERE source_id=? AND status='done'", (sid,)) == len(files)
    # higher-resolution phone copies replace the Takeout copies as the best file (same photo, no duplicate)
    for f in files:
        if "_orig" in f.name and f.name.startswith("IMG_2023"):
            pids = {r[0] for r in db.q("SELECT photo_id FROM files WHERE name=? OR rel LIKE ?", (f.name, "%" + f.name))}
            assert len(pids) == 1, f.name
            best = db.q1("SELECT f.source_id, MAX(p.width, p.height) FROM photos p JOIN files f ON f.id=p.best_file WHERE p.id=?", (pids.pop(),))
            assert best[0] == sid and best[1] == 2400
    # phone originals are not kept on the PC after analysis
    spool = list((env["data"] / "spool").rglob("*"))
    assert not [p for p in spool if p.is_file()], spool
    idx = lan.get("/api/device/index", headers=H).json()
    assert len(idx["photos"]) == len(files)
    assert all("mid" in p and "meta" in p for p in idx["photos"])
    assert any(p["me"] and p["bd"] and p["fb"] for p in idx["photos"])
    # search on the phone: model files with checksums, and the binary search index
    import hashlib
    import struct

    ping = lan.get("/api/device/ping", headers=H).json()
    assert ping["search"]["version"] == "mclip-b32-v1"
    m = lan.get("/api/device/model/dense", headers=H)
    assert m.headers["x-sha256"] == hashlib.sha256(m.content).hexdigest() == ping["search"]["files"]["dense"]
    assert len(m.content) == 512 * 768 * 4
    assert lan.get("/api/device/model/other", headers=H).status_code == 404
    blob = lan.get("/api/device/search-index", headers=H).content
    assert blob[:4] == b"RGV1"
    n, dim = struct.unpack("<II", blob[4:12])
    assert dim == 512 and n == len(idx["photos"]) and len(blob) == 12 + n * 8 + n * dim * 2
    # a photo deleted on the phone becomes "missing"
    r = lan.post("/api/device/manifest", json={"full": True, "items": items[1:]}, headers=H).json()
    assert r["missing"] == 1
    # the phone app's PC tab: device session cookie gives the web UI
    s = TestClient(server.app, client=LAN)
    r = s.get(f"/api/device/session?token={tok}", follow_redirects=False)
    assert r.status_code in (302, 307)
    s.cookies.set("rg_session", r.cookies.get("rg_session"))
    assert s.get("/api/index").status_code == 200
    assert pipeline.ACTIVE_POOL is None


def test_originals_unchanged(env):
    assert tree_hash(env["lib"] / "takeout") == env["before"]
    assert tree_hash(env["lib"] / "phone") == env["before_phone"]


def test_js_filters(env):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    c = env["client"]
    idx = c.get("/api/index").json()
    p = Path(__file__).parent / "filters_check.mjs"
    out = subprocess.run([node, str(p)], input=json.dumps(idx), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr

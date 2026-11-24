"""FastAPI server: web UI, PC API, phone sync API."""
from __future__ import annotations

import io
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import (__version__, auth, benchmark, catalog, config, db, devices, imaging, models, people, pipeline,
               ranking, search)
from .tasks import runner

log = logging.getLogger("ratedgallery.server")
from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    _startup()
    yield


app = FastAPI(title="Rated Gallery", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2000)
RESTART = {"requested": False, "callback": None}
RUNTIME = {"port": None}  # the port the server really listens on (--port can differ from the setting)

PUBLIC = {"/login", "/api/login", "/api/device/pair", "/static/style.css", "/favicon.ico"}


# ---------------------------------------------------------------- startup

def register_tasks():
    runner.register("scan", pipeline.scan_job)
    runner.register("train", ranking.train_job)
    runner.register("match", _match_job)
    runner.register("cluster", lambda p: people.cluster(p))
    runner.register("score", lambda p: ranking.score_all(p))
    runner.register("export", _export_job)
    runner.register("benchmark", lambda p: benchmark.run())
    runner.register("search_index", lambda p: search.index_job(p))


def _match_job(p):
    p.set_phase("Matching faces")
    people.match()
    p.set_phase("Grouping other faces")
    people.cluster(p)
    if people.me_id() is not None:
        pipeline.scan_job(p, list_sources=False)


def _startup():
    register_tasks()
    runner.start()
    db.conn()
    if not models.missing_models() and pipeline.need_hair_update():
        runner.request("scan", list_sources=False)  # add hair measurements to photos analyzed before
    if ranking.label_counts()[0] >= 8 and ranking.current_model()[1] is None:
        runner.request("train")  # the saved model has an old feature layout: train again with the same ratings
    if search.ready() and db.val("SELECT COUNT(*) FROM photos p LEFT JOIN photo_vec v ON v.photo_id=p.id "
                                 "WHERE v.photo_id IS NULL", default=0):
        runner.request("search_index")  # photos from before search existed
    if not models.missing_models() and not os.environ.get("RG_NO_BENCHMARK"):
        runner.request("benchmark")  # every start: shows the real speed (photos per minute) on this PC


# ---------------------------------------------------------------- auth middleware

@app.middleware("http")
async def guard(request: Request, call_next):
    host = request.client.host if request.client else None
    path = request.url.path
    if auth.is_local(host) or path in PUBLIC:
        return await call_next(request)
    if not config.settings()["lan_enabled"]:
        return JSONResponse({"detail": "LAN access is off"}, status_code=403)
    if path.startswith("/api/device/"):
        if path == "/api/device/session":
            return await call_next(request)
        tok = request.headers.get("authorization", "")
        dev = auth.device_for_token(tok[7:] if tok.lower().startswith("bearer ") else None)
        if dev is None:
            return JSONResponse({"detail": "invalid token"}, status_code=401)
        request.state.device = dev
        return await call_next(request)
    if auth.check_session(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return RedirectResponse("/login")


def _device(request: Request):
    dev = getattr(request.state, "device", None)
    if dev is None:  # localhost callers (tests) still need a token
        tok = request.headers.get("authorization", "")
        dev = auth.device_for_token(tok[7:] if tok.lower().startswith("bearer ") else None)
    if dev is None:
        raise HTTPException(401, "invalid token")
    return dev


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return (config.STATIC / "login.html").read_text("utf-8")


@app.post("/api/login")
def login(request: Request, body: dict = Body(...)):
    ip = request.client.host if request.client else "?"
    if auth.too_many_failures(ip):
        raise HTTPException(429, "Too many attempts. Wait 10 minutes.")
    stored = config.settings()["lan_password_hash"]
    if not stored or not auth.check_password(str(body.get("password", "")), stored):
        auth.note_failure(ip)
        raise HTTPException(403, "Wrong password")
    r = JSONResponse({"ok": True})
    r.set_cookie(auth.COOKIE, auth.make_session(), max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax")
    return r


# ---------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def index_page():
    return FileResponse(config.STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=str(config.STATIC)), name="static")


# ---------------------------------------------------------------- status and settings

def lan_addresses():
    out = []
    try:
        import ipaddress

        import psutil

        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            for a in addrs:
                if a.family.name == "AF_INET":
                    ip = ipaddress.ip_address(a.address)
                    if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                        virtual = any(v in name.lower() for v in ("vethernet", "virtualbox", "vmware", "wsl", "docker", "vbox", "hyper-v", "veth", "br-"))
                        out.append({"ip": a.address, "iface": name, "virtual": virtual})
    except Exception:  # noqa: BLE001
        pass
    out.sort(key=lambda d: (d["virtual"], not d["ip"].startswith("192.168."), not d["ip"].startswith("10.")))
    return out


@app.get("/api/status")
def status():
    s = config.settings()
    addrs = lan_addresses()
    port = RUNTIME["port"] or s["port"]
    return {"version": __version__, "tasks": runner.status(), "stats": catalog.stats(),
            "models": models.DEVICES, "missing_models": models.missing_models(),
            "benchmark": db.kv_get("benchmark"), "settings": s.public(), "devices": devices.devices(),
            "sources": [dict(r) for r in db.q("SELECT id, kind, name, root, enabled FROM sources ORDER BY id")],
            "lan": {"enabled": s["lan_enabled"], "addresses": addrs, "port": port,
                    "url": f"http://{addrs[0]['ip']}:{port}/" if addrs else None},
            "model": ranking.quality(), "restart_needed": RESTART["requested"]}


@app.get("/api/settings")
def get_settings():
    return config.settings().public()


EDITABLE = {"workers": int, "low_priority": bool, "map_enabled": bool, "preview_cache_mb": int,
            "match_accept": float, "match_reject": float, "retrain_every": int, "port": int}


@app.post("/api/settings")
def set_settings(body: dict = Body(...)):
    upd = {}
    for k, typ in EDITABLE.items():
        if k in body:
            try:
                upd[k] = typ(body[k])
            except (TypeError, ValueError):
                raise HTTPException(400, f"bad value for {k}")
    if "workers" in upd:
        upd["workers"] = max(1, min(32, upd["workers"]))
    if "preview_cache_mb" in upd:
        upd["preview_cache_mb"] = max(50, upd["preview_cache_mb"])
    config.settings().update(**upd)
    if "low_priority" in upd and pipeline.ACTIVE_POOL is not None:
        pipeline.ACTIVE_POOL.set_low(upd["low_priority"])
    if "preview_cache_mb" in upd:
        pipeline.enforce_preview_cap()
    restart = "port" in upd
    if any(k in upd for k in ("match_accept", "match_reject")):
        runner.request("match")
    return {"ok": True, "settings": config.settings().public(), "restart": restart}


@app.post("/api/settings/lan")
def set_lan(request: Request, body: dict = Body(...)):
    if not auth.is_local(request.client.host if request.client else None):
        raise HTTPException(403, "Change LAN access on the PC only")
    enabled = bool(body.get("enabled"))
    pw = body.get("password")
    s = config.settings()
    if pw:
        if len(pw) < 6:
            raise HTTPException(400, "Use a password with 6 or more characters")
        s.update(lan_password_hash=auth.hash_password(pw), session_secret=os.urandom(32).hex())
    if enabled and not s["lan_password_hash"]:
        raise HTTPException(400, "Set a password first")
    changed = enabled != s["lan_enabled"]
    s.update(lan_enabled=enabled)
    if changed:
        _schedule_restart()
    return {"ok": True, "restarting": changed}


def _schedule_restart():
    RESTART["requested"] = True
    cb = RESTART.get("callback")
    if cb:
        threading.Timer(0.5, cb).start()


@app.get("/api/lan/qr.svg")
def lan_qr(text: str | None = None):
    import qrcode
    import qrcode.image.svg

    st = status()
    data = text or st["lan"]["url"] or "http://127.0.0.1"
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    b = io.BytesIO()
    img.save(b)
    return Response(b.getvalue(), media_type="image/svg+xml")


# ---------------------------------------------------------------- sources and tasks

@app.post("/api/sources")
def add_source(body: dict = Body(...)):
    try:
        sid = pipeline.add_folder(str(body.get("path", "")), body.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": sid}


@app.delete("/api/sources/{sid}")
def remove_source(sid: int):
    db.ex("UPDATE sources SET enabled=0 WHERE id=?", (sid,))
    return {"ok": True}


@app.post("/api/scan")
def scan():
    if models.missing_models():
        raise HTTPException(400, "Models are missing. Run install.bat first.")
    runner.request("scan")
    return {"ok": True}


@app.post("/api/tasks/cancel")
def cancel():
    runner.cancel()
    return {"ok": True}


@app.post("/api/tasks/{name}")
def start_task(name: str):
    if name not in ("match", "cluster", "score", "train", "benchmark"):
        raise HTTPException(404)
    runner.request(name)
    return {"ok": True}


# ---------------------------------------------------------------- images

@app.get("/api/photo/{pid}/thumb")
def thumb(pid: int):
    rel = db.val("SELECT thumb FROM photos WHERE id=?", (pid,))
    p = config.cache_dir("thumbs") / rel if rel else None
    if not p or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "public, max-age=604800, immutable"})


@app.get("/api/face/{fid}/img")
def face_img(fid: int):
    p = pipeline.face_crop_path(fid)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


def _view_image(pid: int, wait_phone: float = 20.0):
    """Large image for a photo: cached preview, else decode a readable original, else ask the phone."""
    r = db.q1("SELECT path FROM previews WHERE photo_id=?", (pid,))
    if r and os.path.exists(r[0]):
        db.ex("UPDATE previews SET used=? WHERE photo_id=?", (time.time(), pid))
        from PIL import Image

        return Image.open(r[0]).convert("RGB"), "preview"
    best = db.val("SELECT best_file FROM photos WHERE id=?", (pid,))
    path, _ = pipeline._readable_file(pid, best)  # noqa: SLF001
    if path is not None:
        im, _ = imaging.decode(Path(path).read_bytes(), 2048)
        return im, "original"
    if devices.request_view(pid):
        end = time.time() + wait_phone
        while time.time() < end:
            time.sleep(0.5)
            r = db.q1("SELECT path FROM previews WHERE photo_id=?", (pid,))
            if r and os.path.exists(r[0]):
                from PIL import Image

                return Image.open(r[0]).convert("RGB"), "phone"
    return None, "unavailable"


@app.get("/api/photo/{pid}/view")
def view(pid: int):
    im, how = _view_image(pid)
    if im is None:
        rel = db.val("SELECT thumb FROM photos WHERE id=?", (pid,))
        if rel and (config.cache_dir("thumbs") / rel).exists():
            return FileResponse(config.cache_dir("thumbs") / rel, media_type="image/webp",
                                headers={"X-RG-Fallback": "thumbnail", "Cache-Control": "no-store"})
        raise HTTPException(404)
    return Response(imaging.jpeg_bytes(im, 2048, 85), media_type="image/jpeg",
                    headers={"X-RG-Source": how, "Cache-Control": "private, max-age=3600"})


@app.get("/api/face/{fid}/large")
def face_large(fid: int):
    f = db.q1("SELECT photo_id, x1, y1, x2, y2 FROM faces WHERE id=?", (fid,))
    if not f:
        raise HTTPException(404)
    im, _ = _view_image(f[0], wait_phone=10)
    if im is None:
        return face_img(fid)
    crop = imaging.square_crop(im, [f[1], f[2], f[3], f[4]], 1.7, 640)
    return Response(imaging.jpeg_bytes(crop, None, 88), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/face/{fid}/head")
def face_head(fid: int):
    """The whole head with the hair (for the hair A/B question)."""
    f = db.q1("SELECT photo_id, x1, y1, x2, y2 FROM faces WHERE id=?", (fid,))
    if not f:
        raise HTTPException(404)
    im, _ = _view_image(f[0], wait_phone=10)
    if im is None:
        return face_img(fid)
    crop = imaging.square_crop(im, models.head_box([f[1], f[2], f[3], f[4]]), 1.0, 640)
    return Response(imaging.jpeg_bytes(crop, None, 88), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/photo/{pid}/original")
def original(pid: int):
    best = db.val("SELECT best_file FROM photos WHERE id=?", (pid,))
    path, frow = pipeline._readable_file(pid, best)  # noqa: SLF001
    if path is None:
        raise HTTPException(409, "This photo is only on the phone. Open it in the phone app.")
    return FileResponse(path, filename=frow["name"] or Path(frow["rel"]).name)


# ---------------------------------------------------------------- catalog

@app.get("/api/index")
def index():
    return catalog.build_index()


@app.get("/api/photo/{pid}")
def photo(pid: int):
    d = catalog.photo_details(pid)
    if d is None:
        raise HTTPException(404)
    return d


@app.post("/api/export")
def export(body: dict = Body(...)):
    ids = [int(i) for i in body.get("photo_ids", [])]
    folder = str(body.get("folder", "")).strip()
    if not ids or not folder:
        raise HTTPException(400, "Select photos and a folder")
    runner.request("export", ids=tuple(ids), folder=folder)
    return {"ok": True, "count": len(ids)}


def _export_job(p, ids, folder):
    dst = Path(folder).expanduser()
    dst.mkdir(parents=True, exist_ok=True)
    p.set_phase("Copying photos", len(ids))
    copied, skipped = 0, 0
    for pid in ids:
        best = db.val("SELECT best_file FROM photos WHERE id=?", (pid,))
        path, frow = pipeline._readable_file(pid, best)  # noqa: SLF001
        if path is None:
            skipped += 1
        else:
            name = frow["name"] or Path(frow["rel"]).name
            target = dst / name
            k = 1
            while target.exists():
                target = dst / f"{Path(name).stem}_{k}{Path(name).suffix}"
                k += 1
            shutil.copy2(path, target)
            copied += 1
        p.step()
    p.message = f"Copied {copied} photos to {dst}" + (f"; {skipped} are only on the phone" if skipped else "")


@app.get("/api/search")
def search_photos(q: str = "", limit: int = 300):
    if not search.ready():
        raise HTTPException(409, "The search model is missing. Run install.bat.")
    res = search.search(q, max(1, min(2000, limit)))
    pending = db.val("SELECT COUNT(*) FROM photos p LEFT JOIN photo_vec v ON v.photo_id=p.id WHERE v.photo_id IS NULL", default=0)
    return {"results": res, "not_indexed": pending}


# ---------------------------------------------------------------- people

@app.get("/api/people")
def people_list():
    return {"persons": people.persons(), "me": people.me_id()}


@app.get("/api/clusters")
def clusters(limit: int = 60):
    return {"clusters": people.clusters(limit), "unclustered": people.unclustered_faces(60)}


@app.get("/api/clusters/{cid}")
def cluster_faces(cid: int):
    return {"faces": people.cluster_faces(cid)}


@app.post("/api/clusters/merge")
def merge(body: dict = Body(...)):
    people.merge_clusters(body.get("ids", []))
    return {"ok": True}


@app.post("/api/clusters/split")
def split(body: dict = Body(...)):
    return {"cluster": people.split_cluster(body.get("face_ids", []))}


@app.post("/api/persons")
def new_person(body: dict = Body(...)):
    faces = body.get("face_ids", [])
    if not faces:
        raise HTTPException(400, "Select faces")
    pid = people.create_person(str(body.get("name") or "").strip() or "Person", faces, bool(body.get("is_me")))
    _after_people_change()
    return {"id": pid}


@app.post("/api/persons/{pid}/faces")
def person_faces(pid: int, body: dict = Body(...)):
    action = body.get("action")
    faces = body.get("face_ids", [])
    if action == "add":
        people.add_faces(pid, faces, "ref")
    elif action == "confirm":
        people.add_faces(pid, faces, "confirmed")
    elif action == "reject":
        people.reject_faces(pid, faces)
    else:
        raise HTTPException(400, "action must be add, confirm or reject")
    _after_people_change(pid)
    return {"ok": True}


def _after_people_change(pid=None):
    if pid is None or pid == people.me_id():
        runner.request("scan", list_sources=False)  # analyze new photos of me, then score


@app.post("/api/persons/{pid}")
def edit_person(pid: int, body: dict = Body(...)):
    if "name" in body:
        people.rename_person(pid, str(body["name"]).strip() or "Person")
    return {"ok": True}


@app.delete("/api/persons/{pid}")
def del_person(pid: int):
    people.delete_person(pid)
    return {"ok": True}


@app.get("/api/persons/{pid}/uncertain")
def person_uncertain(pid: int, limit: int = 24):
    return {"faces": [{"id": r[0], "photo_id": r[1], "sim": round(r[2], 3)} for r in people.uncertain(pid, limit)]}


@app.get("/api/persons/{pid}/faces")
def person_face_list(pid: int, limit: int = 200):
    rows = db.q(f"SELECT id, assign FROM faces WHERE person_id=? AND assign IN {people.ASSIGNED} "
                "ORDER BY (assign='ref') DESC, sim DESC LIMIT ?", (pid, limit))
    return {"faces": [{"id": r[0], "assign": r[1]} for r in rows]}


# ---------------------------------------------------------------- calibration

CALIB = {"queue": [], "n": 0, "lock": threading.Lock()}


def _item(fid: int):
    r = db.q1("""SELECT f.id, f.photo_id, f.x1, f.y1, f.x2, f.y2, p.width, p.height, s.score, s.conf, s.prior
                 FROM faces f JOIN photos p ON p.id=f.photo_id LEFT JOIN scores s ON s.face_id=f.id WHERE f.id=?""", (fid,))
    if not r:
        return None
    return {"face_id": r[0], "photo_id": r[1], "box": [r[2], r[3], r[4], r[5]], "w": r[6], "h": r[7],
            "score": r[8], "conf": r[9], "prior": bool(r[10]) if r[10] is not None else None,
            "rating": ranking.current_ratings().get(r[0])}


@app.get("/api/calib/next")
def calib_next(skip_pair: bool = False):
    with CALIB["lock"]:
        CALIB["n"] += 1
        if CALIB["n"] % 5 == 0 and not skip_pair:
            pr = ranking.select_pair()
            if pr:
                return {"kind": "pair", "a": _item(pr[0]), "b": _item(pr[1]), "model": ranking.quality()}
        if CALIB["n"] % 10 == 7 and not skip_pair:  # sometimes: which photo has better hair?
            pr = ranking.select_pair(kind="hair")
            if pr:
                return {"kind": "hairpair", "a": _item(pr[0]), "b": _item(pr[1]), "model": ranking.quality()}
        rated = set(ranking.current_ratings())
        skipped = {r[0] for r in db.q("SELECT face_id FROM skips WHERE active=1")}
        CALIB["queue"] = [f for f in CALIB["queue"] if f not in rated and f not in skipped]
        if not CALIB["queue"]:
            CALIB["queue"] = ranking.select_batch(10)
        while CALIB["queue"]:
            it = _item(CALIB["queue"].pop(0))
            if it:
                return {"kind": "rate", "item": it, "model": ranking.quality()}
    return {"kind": "done", "model": ranking.quality()}


def _action(kind, row_id):
    db.ex("INSERT INTO actions(kind,row_id,created) VALUES(?,?,?)", (kind, row_id, time.time()))


def _maybe_retrain():
    if ranking.labels_since_model() >= int(config.settings()["retrain_every"]):
        runner.request("train")


@app.post("/api/calib/rate")
def calib_rate(body: dict = Body(...)):
    fid = int(body["face_id"])
    rating = int(body["rating"])
    if not 1 <= rating <= 10:
        raise HTTPException(400, "rating must be 1..10")
    pid = db.val("SELECT photo_id FROM faces WHERE id=?", (fid,))
    if pid is None:
        raise HTTPException(404)
    if db.val("SELECT me_face FROM photos WHERE id=?", (pid,)) != fid:
        raise HTTPException(400, "Only photos of me can be rated")
    rid = db.ex("INSERT INTO ratings(face_id,photo_id,rating,source,created) VALUES(?,?,?,?,?)",
                (fid, pid, rating, body.get("source", "calib"), time.time())).lastrowid
    hair = int(body.get("hair") or 0)
    if hair in (1, -1):  # optional: hair good / hair bad (undo removes it with the rating)
        db.ex("INSERT INTO hair_tags(face_id,tag,rating_id,created) VALUES(?,?,?,?)", (fid, hair, rid, time.time()))
    _action("rate", rid)
    pipeline.drop_preview(pid)  # a rated photo does not need the calibration preview any more
    _maybe_retrain()
    return {"ok": True, "model": ranking.quality()}


@app.post("/api/calib/compare")
def calib_compare(body: dict = Body(...)):
    a, b, res = int(body["a"]), int(body["b"]), str(body["result"])
    kind = str(body.get("kind") or "overall")
    if res not in ("a", "b", "eq") or kind not in ("overall", "hair"):
        raise HTTPException(400, "result must be a, b or eq; kind overall or hair")
    rid = db.ex("INSERT INTO comparisons(face_a,face_b,result,created,kind) VALUES(?,?,?,?,?)",
                (a, b, res, time.time(), kind)).lastrowid
    _action("compare", rid)
    _maybe_retrain()
    return {"ok": True, "model": ranking.quality()}


@app.post("/api/calib/skip")
def calib_skip(body: dict = Body(...)):
    rid = db.ex("INSERT INTO skips(face_id,created) VALUES(?,?)", (int(body["face_id"]), time.time())).lastrowid
    _action("skip", rid)
    return {"ok": True}


@app.post("/api/calib/undo")
def calib_undo():
    r = db.q1("SELECT id, kind, row_id FROM actions ORDER BY id DESC LIMIT 1")
    if not r:
        return {"ok": False}
    table = {"rate": "ratings", "compare": "comparisons", "skip": "skips"}[r["kind"]]
    db.ex(f"UPDATE {table} SET active=0 WHERE id=?", (r["row_id"],))
    if r["kind"] == "rate":
        db.ex("UPDATE hair_tags SET active=0 WHERE rating_id=?", (r["row_id"],))
    db.ex("DELETE FROM actions WHERE id=?", (r["id"],))
    if r["kind"] == "compare":
        c = db.q1("SELECT face_a, face_b, COALESCE(kind,'overall') FROM comparisons WHERE id=?", (r["row_id"],))
        return {"ok": True, "redo": {"kind": "hairpair" if c[2] == "hair" else "pair", "a": _item(c[0]), "b": _item(c[1])}}
    fid = db.val(f"SELECT face_id FROM {table} WHERE id=?", (r["row_id"],))
    return {"ok": True, "redo": {"kind": "rate", "item": _item(fid)}}


@app.get("/api/model")
def model_quality():
    return ranking.quality()


@app.get("/api/calib/surprising")
def calib_surprising():
    out = []
    for d in ranking.surprising(30):
        it = _item(d["face_id"])
        if it:
            out.append({**d, "item": it})
    return {"items": out}


# ---------------------------------------------------------------- phone pairing (PC side)

@app.post("/api/pair/start")
def pair_start(request: Request):
    if not auth.is_local(request.client.host if request.client else None):
        raise HTTPException(403, "Start pairing on the PC")
    if not config.settings()["lan_enabled"]:
        raise HTTPException(400, "Turn on LAN access first")
    p = devices.start_pairing()
    st = status()
    addr = st["lan"]["addresses"][0]["ip"] if st["lan"]["addresses"] else "127.0.0.1"
    link = f"ratedgallery://pair?host={addr}&port={st['lan']['port']}&code={p['code']}"
    return {"code": p["code"], "expires": p["expires"], "link": link, "host": addr, "port": st["lan"]["port"]}


@app.delete("/api/devices/{did}")
def device_remove(did: int):
    devices.remove_device(did)
    return {"ok": True}


# ---------------------------------------------------------------- phone sync API (docs/API.md)

@app.post("/api/device/pair")
def device_pair(body: dict = Body(...)):
    r = devices.pair(str(body.get("code", "")), str(body.get("name", "Phone")))
    if r is None:
        raise HTTPException(403, "invalid or expired code")
    return r


@app.get("/api/device/ping")
def device_ping(request: Request):
    import socket

    dev = _device(request)
    return {"ok": True, "server_version": __version__, "server_name": socket.gethostname(), "device_id": dev["id"],
            "search": {"version": search.MODEL_VERSION, "files": _search_file_shas()} if search.ready() else None}


_SHA_CACHE: dict = {}


def _search_file_shas():
    import hashlib

    out = {}
    for name, fname in search.TEXT_FILES.items():
        p = config.MODELS / fname
        key = (str(p), p.stat().st_mtime, p.stat().st_size)
        if key not in _SHA_CACHE:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            _SHA_CACHE[key] = h.hexdigest()
        out[name] = _SHA_CACHE[key]
    return out


@app.get("/api/device/model/{name}")
def device_model(name: str, request: Request):
    _device(request)
    if name not in search.TEXT_FILES or not search.ready():
        raise HTTPException(404)
    return FileResponse(config.MODELS / search.TEXT_FILES[name], media_type="application/octet-stream",
                        headers={"X-Sha256": _search_file_shas()[name]})


@app.get("/api/device/search-index")
def device_search_index(request: Request):
    dev = _device(request)
    return Response(search.device_blob(dev["source_id"]), media_type="application/octet-stream")


@app.post("/api/device/manifest")
def device_manifest(request: Request, body: dict = Body(...)):
    dev = _device(request)
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(400, "items must be a list")
    return devices.manifest(dev, items, bool(body.get("full")))


@app.get("/api/device/wanted")
def device_wanted(request: Request, limit: int = 20):
    return devices.wanted(_device(request), max(1, min(200, limit)))


@app.put("/api/device/file/{mid}")
async def device_file(mid: str, request: Request):
    dev = _device(request)
    if devices.file_row(dev, mid) is None:
        raise HTTPException(404, "unknown mid; send the manifest first")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > 400 * 1024 * 1024:
            raise HTTPException(413, "file too large")
    ok = await _run_sync(devices.receive, dev, mid, [bytes(body)])
    return {"ok": ok}


async def _run_sync(fn, *a):
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(fn, *a)


@app.post("/api/device/ratings")
def device_ratings(request: Request, body: dict = Body(...)):
    """Ratings made on the phone (offline). Idempotent: the same item sent again makes no second rating."""
    _device(request)
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(400, "items must be a list")
    accepted, rejected = [], []
    for it in items[:10000]:
        try:
            pid, rating, created = int(it["photo_id"]), int(it["rating"]), float(it["created"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            rejected.append({"photo_id": it.get("photo_id") if isinstance(it, dict) else None, "reason": "bad item"})
            continue
        if not 1 <= rating <= 10:
            rejected.append({"photo_id": pid, "reason": "rating must be 1..10"})
            continue
        fid = db.val("SELECT me_face FROM photos WHERE id=?", (pid,))
        if fid is None:
            rejected.append({"photo_id": pid, "reason": "not a photo of me"})
            continue
        if not db.val("SELECT 1 FROM ratings WHERE face_id=? AND source='phone' AND created=? AND rating=?",
                      (fid, created, rating)):
            db.ex("INSERT INTO ratings(face_id,photo_id,rating,source,created) VALUES(?,?,?,'phone',?)",
                  (fid, pid, rating, created))
            pipeline.drop_preview(pid)
        accepted.append(pid)
    if accepted:
        _maybe_retrain()
    return {"accepted": accepted, "rejected": rejected}


@app.get("/api/device/status")
def device_status(request: Request):
    return devices.status(_device(request))


@app.get("/api/device/index")
def device_index(request: Request):
    dev = _device(request)
    return catalog.build_index(device_source=dev["source_id"])


@app.get("/api/device/session")
def device_session(token: str = ""):
    dev = auth.device_for_token(token)
    if dev is None:
        raise HTTPException(401, "invalid token")
    r = RedirectResponse("/")
    r.set_cookie(auth.COOKIE, auth.make_session("dev"), max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax")
    return r

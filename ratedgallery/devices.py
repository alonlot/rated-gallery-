"""Phone sync (see docs/API.md). The phone is the client; uploads go to data/spool until analyzed."""
from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from pathlib import Path

from . import auth, config, db, pipeline

_pair = {"code": None, "expires": 0.0}
_lock = threading.Lock()
WANT_SCAN, WANT_FEATURES, WANT_VIEW = 1, 2, 3


def start_pairing() -> dict:
    with _lock:
        _pair["code"] = f"{secrets.randbelow(10**6):06d}"
        _pair["expires"] = time.time() + 600
        return dict(_pair)


def pair(code: str, name: str) -> dict | None:
    with _lock:
        if not _pair["code"] or time.time() > _pair["expires"] or not secrets.compare_digest(str(code), _pair["code"]):
            return None
        _pair["code"] = None
    token = secrets.token_hex(32)
    name = (name or "Phone")[:60]
    with db.tx() as c:
        did = c.execute("INSERT INTO devices(name, token_hash, created, last_seen) VALUES(?,?,?,?)",
                        (name, auth.token_hash(token), time.time(), time.time())).lastrowid
        sid = c.execute("INSERT INTO sources(kind, name, device_id, added) VALUES('device',?,?,?)",
                        (name, did, time.time())).lastrowid
        c.execute("UPDATE devices SET source_id=? WHERE id=?", (sid, did))
    return {"device_id": did, "token": token, "server_name": socket.gethostname()}


def manifest(dev, items: list, full: bool) -> dict:
    sid = dev["source_id"]
    existing = {r[0]: r for r in db.q("SELECT rel, id, size, mtime, status, photo_id FROM files WHERE source_id=?", (sid,))}
    seen, ins, upd = set(), [], []
    n_known = n_changed = 0
    for it in items:
        mid = str(it.get("mid", "")).strip()
        if not mid:
            continue
        seen.add(mid)
        name = str(it.get("name") or mid)[:300]
        size, mtime = int(it.get("size") or 0), float(it.get("mtime") or 0)
        taken = it.get("taken")
        taken = float(taken) / 1000.0 if taken else None
        mime = it.get("mime")
        old = existing.get(mid)
        if old is None:
            ins.append((sid, mid, name, size, mtime, taken, mime))
        elif old["size"] != size or abs((old["mtime"] or 0) - mtime) > 1:
            upd.append((name, size, mtime, taken, mime, old["id"]))
            n_changed += 1
        elif old["status"] == "missing":
            db.ex("UPDATE files SET status=CASE WHEN photo_id IS NULL THEN 'new' ELSE 'done' END, "
                  "want=CASE WHEN photo_id IS NULL THEN 1 ELSE 0 END WHERE id=?", (old["id"],))
            n_known += 1
        else:
            n_known += 1
    gone = [(r["id"],) for m, r in existing.items() if full and m not in seen and r["status"] != "missing"]
    with db.tx() as c:
        c.executemany("INSERT INTO files(source_id, rel, name, size, mtime, taken_hint, mime, status, want) "
                      "VALUES(?,?,?,?,?,?,?,'new',1)", ins)
        c.executemany("UPDATE files SET name=?, size=?, mtime=?, taken_hint=?, mime=?, status='new', want=1, attempts=0 "
                      "WHERE id=?", upd)
        c.executemany("UPDATE files SET status='missing', want=0 WHERE id=?", gone)
    pipeline.update_missing_flags()
    return {"known": n_known, "new": len(ins), "changed": n_changed, "missing": len(gone)}


def wanted(dev, limit=20) -> dict:
    sid = dev["source_id"]
    rows = db.q("""SELECT id, rel, want FROM files WHERE source_id=? AND attempts<3 AND status!='missing' AND
                   (want>0 OR status='new') ORDER BY want DESC, id LIMIT ?""", (sid, int(limit) * 3))
    items = []
    for r in rows:
        if pipeline.spool_path(sid, r["id"]).exists():
            continue  # already uploaded, waiting for analysis
        items.append(r["rel"])
        if len(items) >= limit:
            break
    remaining = db.val("SELECT COUNT(*) FROM files WHERE source_id=? AND attempts<3 AND status!='missing' "
                       "AND (want>0 OR status='new')", (sid,), default=0)
    return {"items": items, "remaining": max(0, remaining - len(items))}


def file_row(dev, mid: str):
    return db.q1("SELECT * FROM files WHERE source_id=? AND rel=?", (dev["source_id"], str(mid)))


def receive(dev, mid: str, chunks) -> bool:
    """Store one upload. chunks: iterable of bytes. Returns False if the mid is unknown."""
    r = file_row(dev, mid)
    if r is None:
        return False
    dst = pipeline.spool_path(r["source_id"], r["id"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part")
    with open(tmp, "wb") as f:
        for ch in chunks:
            f.write(ch)
    os.replace(tmp, dst)
    want = r["want"] or 0
    if want == WANT_VIEW:
        make_view_preview(r, dst)
    if r["status"] in ("new", "error"):
        db.ex("UPDATE files SET status='uploaded', want=0 WHERE id=?", (r["id"],))
    else:
        db.ex("UPDATE files SET want=0 WHERE id=?", (r["id"],))
        if want != WANT_FEATURES:  # view only: the original is not needed any more
            try:
                dst.unlink()
            except OSError:
                pass
    from .tasks import runner

    runner.request("scan", list_sources=False)
    return True


def make_view_preview(frow, path: Path):
    from . import imaging

    pid = frow["photo_id"]
    if not pid:
        return
    try:
        im, _ = imaging.decode(path.read_bytes(), 2048)
        out = pipeline.preview_path(pid)
        n = imaging.save_jpeg(im, out, 2048, 85)
        db.ex("INSERT OR REPLACE INTO previews(photo_id,path,bytes,used) VALUES(?,?,?,?)", (pid, str(out), n, time.time()))
        pipeline.enforce_preview_cap()
    except Exception:  # noqa: BLE001
        pass


def request_view(photo_id: int) -> bool:
    """Ask the phone for a photo the user opened on the PC. Returns False if no phone file exists."""
    r = db.q1("""SELECT f.id FROM files f JOIN sources s ON s.id=f.source_id WHERE f.photo_id=? AND s.kind='device'
                 AND f.status!='missing' ORDER BY f.width*f.height DESC LIMIT 1""", (photo_id,))
    if not r:
        return False
    db.ex("UPDATE files SET want=MAX(want,3) WHERE id=?", (r[0],))
    return True


def status(dev) -> dict:
    from .tasks import runner

    pending = db.val("SELECT COUNT(*) FROM files WHERE source_id=? AND status='uploaded'", (dev["source_id"],), default=0)
    st = runner.status()
    cur = st["current"] or {}
    return {"pending": pending, "busy": bool(cur), "task": cur.get("phase") or cur.get("name") or "",
            "done": cur.get("done", 0), "total": cur.get("total", 0)}


def devices():
    rows = db.q("SELECT d.id, d.name, d.created, d.last_seen, d.source_id, "
                "(SELECT COUNT(*) FROM files f WHERE f.source_id=d.source_id AND f.status!='missing') AS n_files, "
                "(SELECT COUNT(*) FROM files f WHERE f.source_id=d.source_id AND (f.status='new' OR f.want>0)) AS n_wanted "
                "FROM devices d ORDER BY d.id")
    now = time.time()
    return [{**dict(r), "connected": bool(r["last_seen"] and now - r["last_seen"] < 30)} for r in rows]


def remove_device(did: int):
    """Unpair. Photos stay; the phone's files become 'missing' only through a later manifest."""
    db.ex("DELETE FROM devices WHERE id=?", (did,))

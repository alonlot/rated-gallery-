"""Gallery index (shared by the PC web UI and the phone app, schema in docs/API.md) and photo details."""
from __future__ import annotations

import json
import time
from datetime import datetime

from . import db, people, ranking
from .people import ASSIGNED


def camera_name(make, model):
    if not model:
        return make or None
    if make and not model.lower().startswith(make.lower().split()[0]):
        return f"{make} {model}"
    return model


def place_text(r):
    parts = [r["city"], r["admin"], r["country"]]
    out = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return ", ".join(out) or None


def date_text(t):
    if not t:
        return None
    try:
        return datetime.fromisoformat(t).strftime("%d %b %Y %H:%M")
    except ValueError:
        return t


def build_index(device_source: int | None = None) -> dict:
    mid = people.me_id()
    persons = [{"id": r["id"], "name": r["name"], "me": bool(r["is_me"])} for r in db.q("SELECT * FROM persons ORDER BY is_me DESC, name")]
    where = "p.missing=0"
    args: list = []
    mids = {}
    if device_source is not None:
        for pid, rel in db.q("SELECT photo_id, rel FROM files WHERE source_id=? AND status='done' AND photo_id IS NOT NULL",
                             (device_source,)):
            mids[pid] = rel
    rows = db.q(f"""SELECT p.*, s.score, s.conf, s.prior, s.contrib, s.score_p, src.kind AS src_kind,
                    json_extract(fe.data, '$.beauty') AS beauty
                    FROM photos p LEFT JOIN scores s ON s.face_id=p.me_face
                    LEFT JOIN features fe ON fe.face_id=p.me_face
                    LEFT JOIN files bf ON bf.id=p.best_file LEFT JOIN sources src ON src.id=bf.source_id
                    WHERE {where}""", args)
    pmap: dict[int, list] = {}
    for ph, per in db.q(f"SELECT DISTINCT photo_id, person_id FROM faces WHERE person_id IS NOT NULL AND assign IN {ASSIGNED}"):
        pmap.setdefault(ph, []).append(per)
    ratings = ranking.current_ratings()
    boxes = {}
    if device_source is not None:
        for r in db.q("SELECT f.id, f.x1, f.y1, f.x2, f.y2 FROM faces f JOIN photos p ON p.me_face=f.id"):
            boxes[r[0]] = [round(r[1], 4), round(r[2], 4), round(r[3], 4), round(r[4], 4)]
    bvals = [r["beauty"] for r in rows if r["me_face"] is not None and r["beauty"] is not None]
    bmean = sum(bvals) / len(bvals) if bvals else None
    photos = []
    for r in rows:
        if device_source is not None and r["id"] not in mids:
            continue
        has_me = r["me_face"] is not None
        d = {"id": r["id"], "t": r["taken_local"], "lat": _r(r["lat"]), "lon": _r(r["lon"]), "cc": r["cc"],
             "country": r["country"], "admin": r["admin"], "city": r["city"], "cam": camera_name(r["make"], r["model"]),
             "facing": r["facing"] or "unknown", "np": r["n_people"] or 0, "me": has_me,
             "s": round(r["score"], 2) if r["score"] is not None else None,
             "sp": round(r["score_p"], 2) if r["score_p"] is not None else None,
             "c": round(r["conf"], 2) if r["conf"] is not None else None, "prior": bool(r["prior"]),
             "r": ratings.get(r["me_face"]) if has_me else None,
             # general attractiveness model, 1..10, and the difference from my own average (display only)
             "a": round(r["beauty"], 1) if has_me and r["beauty"] is not None else None,
             "ad": round(r["beauty"] - bmean, 1) if has_me and r["beauty"] is not None and bmean is not None else None, "p": sorted(pmap.get(r["id"], [])),
             "w": r["width"], "h": r["height"], "src": "phone" if r["src_kind"] == "device" else "folder", "b": r["burst"]}
        if device_source is not None:
            d["mid"] = mids[r["id"]]
            d["fb"] = boxes.get(r["me_face"])
            d["bd"] = json.loads(r["contrib"]) if r["contrib"] else []
            d["meta"] = {k: v for k, v in (("Date", date_text(r["taken_local"])), ("Place", place_text(r)),
                                           ("Camera", d["cam"]), ("Size", f"{r['width']} x {r['height']}"))
                         if v}
        photos.append(d)
    q = ranking.quality()
    model = {"labels": q["ratings"], "pairs": q["pairs"], "spearman": q["spearman"], "trained": q["trained"]}
    return {"version": 1, "index_version": 2, "generated": datetime.now().isoformat(timespec="seconds"), "model": model,
            "me_person": mid, "persons": persons, "photos": photos}


def _r(v):
    return None if v is None else round(v, 5)


FEATURE_VIEW = [("eye_open", "Eyes open", lambda f: 1 - (f.get("eyeBlinkLeft", 0) + f.get("eyeBlinkRight", 0)) / 2),
                ("smile", "Smile", lambda f: (f.get("mouthSmileLeft", 0) + f.get("mouthSmileRight", 0)) / 2),
                ("jaw", "Mouth open", lambda f: f.get("jawOpen")),
                ("yaw", "Head turn (deg)", lambda f: f.get("yaw")), ("pitch", "Head up/down (deg)", lambda f: f.get("pitch")),
                ("roll", "Head tilt (deg)", lambda f: f.get("roll")), ("sharp", "Face sharpness", lambda f: f.get("sharp")),
                ("face_mean", "Face brightness", lambda f: f.get("face_mean")),
                ("clip_hi", "Blown highlights on face", lambda f: f.get("clip_hi")),
                ("bg_contrast", "Face vs background", lambda f: f.get("bg_contrast")),
                ("face_frac", "Face area of frame", lambda f: f.get("face_frac")),
                ("iqa", "Image quality (TOPIQ)", lambda f: f.get("iqa"))]


def photo_details(pid: int) -> dict | None:
    r = db.q1("""SELECT p.*, s.score, s.conf, s.prior, s.contrib, s.score_p FROM photos p
                 LEFT JOIN scores s ON s.face_id=p.me_face WHERE p.id=?""", (pid,))
    if r is None:
        return None
    files = [dict(x) for x in db.q("""SELECT f.id, f.rel, f.name, f.size, f.status, f.width, f.height, s.kind, s.name AS source
                                      FROM files f JOIN sources s ON s.id=f.source_id WHERE f.photo_id=?""", (pid,))]
    faces = [dict(x) for x in db.q("""SELECT f.id, f.x1, f.y1, f.x2, f.y2, f.det, f.assign, f.sim, f.person_id, pe.name
                                      FROM faces f LEFT JOIN persons pe ON pe.id=f.person_id WHERE f.photo_id=?""", (pid,))]
    for f in faces:
        if f["assign"] not in ASSIGNED:
            f["name"] = None
    feats = {}
    fr = db.q1("SELECT data FROM features WHERE face_id=?", (r["me_face"],)) if r["me_face"] else None
    if fr:
        fd = json.loads(fr[0])
        for key, label, fn in FEATURE_VIEW:
            try:
                v = fn(fd)
            except TypeError:
                v = None
            if v is not None and v == v:
                feats[label] = round(float(v), 3)
    rating = ranking.current_ratings().get(r["me_face"]) if r["me_face"] else None
    meta = {"Date": date_text(r["taken_local"]) or "No date", "Place": place_text(r) or "No location",
            "GPS": f"{r['lat']:.5f}, {r['lon']:.5f}" if r["lat"] is not None else None,
            "Camera": camera_name(r["make"], r["model"]), "Lens": r["lens"],
            "Camera side": {"front": "Front (selfie)", "rear": "Rear"}.get(r["facing"], "Unknown"),
            "Size": f"{r['width']} x {r['height']}", "People": r["n_people"],
            "Faces found": r["n_faces"]}
    return {"id": pid, "me_face": r["me_face"], "score": r["score"], "score_p": r["score_p"], "conf": r["conf"],
            "prior": bool(r["prior"]),
            "breakdown": json.loads(r["contrib"]) if r["contrib"] else [], "rating": rating,
            "meta": {k: v for k, v in meta.items() if v not in (None, "")}, "features": feats, "files": files,
            "faces": faces, "w": r["width"], "h": r["height"], "burst": r["burst"], "t": r["taken_local"],
            "lat": r["lat"], "lon": r["lon"], "readable": _readable(pid, r["best_file"]),
            "has_preview": bool(db.val("SELECT 1 FROM previews WHERE photo_id=?", (pid,)))}


def _readable(pid, best):
    from .pipeline import _readable_file

    p, _ = _readable_file(pid, best)
    return p is not None


def stats() -> dict:
    files = {r[0]: r[1] for r in db.q("SELECT status, COUNT(*) FROM files GROUP BY status")}
    return {"files": files, "photos": db.val("SELECT COUNT(*) FROM photos WHERE missing=0", default=0),
            "faces": db.val("SELECT COUNT(*) FROM faces", default=0),
            "me_photos": db.val("SELECT COUNT(*) FROM photos WHERE me_face IS NOT NULL AND missing=0", default=0),
            "me_analyzed": db.val("SELECT COUNT(*) FROM photos WHERE me_face IS NOT NULL AND stage2=1", default=0),
            "scored": db.val("SELECT COUNT(*) FROM scores", default=0),
            "persons": db.val("SELECT COUNT(*) FROM persons", default=0),
            "previews_mb": round((db.val("SELECT SUM(bytes) FROM previews", default=0) or 0) / 1e6, 1),
            "time": time.time()}

"""Scan pipeline.

Pass 1 (all photos): CPU workers decode + metadata + thumbnail + pHash; the main process runs
SCRFD and ArcFace (GPU) and writes to SQLite in batches.
Pass 2 (only photos where my face is): CPU workers make the full-resolution face crop, MediaPipe,
sharpness and exposure; the main process runs CLIP and TOPIQ (GPU).

Originals are only read. Phone uploads are kept in data/spool until analysis is done, then deleted.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import config, db, geo, imaging, metadata, people
from .tasks import Progress

log = logging.getLogger("ratedgallery.pipeline")
ROUND = 400  # files per round; keeps the phone spool small
ACTIVE_POOL = None  # the running worker pool (for live low-priority changes)


def longpath(p) -> str:
    s = str(Path(p).resolve())
    if sys.platform == "win32" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return s


def spool_path(source_id: int, file_id: int) -> Path:
    return config.DATA / "spool" / str(source_id) / str(file_id)


def face_crop_path(face_id: int) -> Path:
    return config.cache_dir("faces") / str(face_id // 1000) / f"{face_id}.jpg"


def preview_path(photo_id: int) -> Path:
    return config.cache_dir("previews") / f"{photo_id}.jpg"


# ---------------------------------------------------------------- sources

def add_folder(path: str, name: str | None = None) -> int:
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ValueError(f"Not a folder: {path}")
    root = str(p.resolve())
    r = db.q1("SELECT id FROM sources WHERE kind='folder' AND root=?", (root,))
    if r:
        db.ex("UPDATE sources SET enabled=1 WHERE id=?", (r[0],))
        return r[0]
    return db.ex("INSERT INTO sources(kind,name,root,added) VALUES('folder',?,?,?)",
                 (name or p.name, root, db.now())).lastrowid


def list_folder(src, prog: Progress | None = None) -> dict:
    root = Path(src["root"])
    stats = {"new": 0, "changed": 0, "missing": 0, "videos": 0, "seen": 0}
    if not root.is_dir():
        if prog:
            prog.message = f"Folder not found: {root}"
        return stats
    existing = {r[0]: (r[1], r[2], r[3], r[4]) for r in
                db.q("SELECT rel, id, size, mtime, status FROM files WHERE source_id=?", (src["id"],))}
    seen = set()
    ins, upd = [], []
    lroot = longpath(root)
    for dirpath, _dirs, files in os.walk(lroot):
        imgs = [f for f in files if imaging.is_image(f)]
        stats["videos"] += sum(1 for f in files if Path(f).suffix.lower() in imaging.VIDEO_EXT)
        if not imgs:
            continue
        jsons = [Path(dirpath) / f for f in files if f.lower().endswith(".json")]
        idx = metadata.index_sidecars(jsons) if jsons else {}
        for f in imgs:
            full = Path(dirpath) / f
            try:
                st = full.stat()
            except OSError:
                continue
            rel = os.path.relpath(full, lroot).replace("\\", "/")
            seen.add(rel)
            side = metadata.match_sidecar(f, idx) if idx else None
            if side and sys.platform == "win32" and side.startswith("\\\\?\\"):
                side = side[4:]
            old = existing.get(rel)
            if old is None:
                ins.append((src["id"], rel, f, st.st_size, st.st_mtime, side))
            elif old[1] != st.st_size or abs((old[2] or 0) - st.st_mtime) > 1:
                upd.append((st.st_size, st.st_mtime, side, old[0]))
            elif old[3] == "missing":
                upd.append((st.st_size, st.st_mtime, side, old[0]))
        if prog:
            prog.message = f"Listing: {len(seen)} images found"
            prog.check()
    stats["seen"] = len(seen)
    gone = [(existing[r][0],) for r in existing if r not in seen and existing[r][3] != "missing"]
    with db.tx() as c:
        c.executemany("INSERT INTO files(source_id,rel,name,size,mtime,sidecar,status) VALUES(?,?,?,?,?,?,'new')", ins)
        c.executemany("UPDATE files SET size=?, mtime=?, sidecar=?, status='new', attempts=0 WHERE id=?", upd)
        c.executemany("UPDATE files SET status='missing' WHERE id=?", gone)
    stats.update(new=len(ins), changed=len(upd), missing=len(gone))
    update_missing_flags()
    return stats


def update_missing_flags():
    db.ex("UPDATE photos SET missing = NOT EXISTS(SELECT 1 FROM files f WHERE f.photo_id=photos.id AND f.status!='missing')")


def file_path(frow) -> Path | None:
    """A readable path for a files row, or None (phone file not uploaded)."""
    src = db.q1("SELECT kind, root FROM sources WHERE id=?", (frow["source_id"],))
    if src is None:
        return None
    if src["kind"] == "folder":
        p = Path(longpath(Path(src["root"]) / frow["rel"]))
        return p if os.path.exists(p) else None
    sp = spool_path(frow["source_id"], frow["id"])
    return sp if sp.exists() else None


# ---------------------------------------------------------------- pHash index

class HashIndex:
    def __init__(self):
        rows = db.q("SELECT id, phash, width, height, taken_ts, burst FROM photos WHERE phash IS NOT NULL")
        self.ids = [r[0] for r in rows]
        self.h = np.array([int(r[1], 16) for r in rows], dtype=np.uint64)
        self.area = [(r[2] or 0) * (r[3] or 0) for r in rows]
        self.aspect = [((r[2] or 1) / (r[3] or 1)) for r in rows]
        self.ts = [r[4] for r in rows]
        self.burst = [r[5] for r in rows]

    def dist(self, hexh: str):
        if not self.ids:
            return np.zeros(0, dtype=np.int64)
        return np.bitwise_count(self.h ^ np.uint64(int(hexh, 16))).astype(np.int64)

    def add(self, pid, hexh, w, h, ts, burst):
        self.ids.append(pid)
        self.h = np.append(self.h, np.uint64(int(hexh, 16)))
        self.area.append(w * h)
        self.aspect.append(w / max(h, 1))
        self.ts.append(ts)
        self.burst.append(burst)


# ---------------------------------------------------------------- pass 1

def pending_pass1(limit=ROUND):
    return db.q("""SELECT f.* FROM files f JOIN sources s ON s.id=f.source_id
                   WHERE s.enabled=1 AND f.attempts<3 AND
                   ((s.kind='folder' AND f.status='new') OR (s.kind='device' AND f.status='uploaded'))
                   ORDER BY f.id LIMIT ?""", (limit,))


def count_pass1():
    return db.val("""SELECT COUNT(*) FROM files f JOIN sources s ON s.id=f.source_id
                     WHERE s.enabled=1 AND f.attempts<3 AND
                     ((s.kind='folder' AND f.status='new') OR (s.kind='device' AND f.status='uploaded'))""", default=0)


META_FIELDS = ("taken_local", "taken_ts", "lat", "lon", "make", "model", "lens", "focal", "focal35", "orientation")


def _throttle(t0):
    if config.settings()["low_priority"]:
        time.sleep(min(1.0, time.perf_counter() - t0))


def run_pass1(rows, pool, prog: Progress, det, arc, hidx: HashIndex, new_faces: list):
    s = config.settings()
    near, burst_bits, burst_s = int(s["near_dup_bits"]), int(s["burst_bits"]), float(s["burst_seconds"])
    jobs = []
    for r in rows:
        p = file_path(r)
        if p is None:
            db.ex("UPDATE files SET status=CASE WHEN status='uploaded' THEN 'new' ELSE status END, attempts=attempts+1 WHERE id=?", (r["id"],))
            continue
        jobs.append({"file_id": r["id"], "path": str(p), "sidecar": r["sidecar"], "taken_hint": r["taken_hint"]})
    c = db.conn()
    pending_commit = 0
    c.execute("BEGIN IMMEDIATE")
    try:
        for res in pool.imap(_scan_task_ref(), jobs):
            fid = res.get("file_id")
            if not res.get("ok"):
                c.execute("UPDATE files SET status='error', error=?, attempts=attempts+1 WHERE id=?", (res.get("error"), fid))
                log.info("file %s: %s", fid, res.get("error"))
            else:
                _handle_scan(c, res, det, arc, hidx, near, burst_bits, burst_s, new_faces)
            prog.step()
            pending_commit += 1
            if pending_commit >= 25:
                c.execute("COMMIT")
                c.execute("BEGIN IMMEDIATE")
                pending_commit = 0
        c.execute("COMMIT")
    except BaseException:
        c.execute("COMMIT")  # keep finished work; the current file stays 'new' and is retried
        raise


def _scan_task_ref():
    from .workers import scan_task

    return scan_task


def _handle_scan(c, res, det, arc, hidx: HashIndex, near, burst_bits, burst_s, new_faces):
    fid, meta = res["file_id"], res["meta"]
    w, h = res["width"], res["height"]
    c.execute("UPDATE files SET sha256=?, width=?, height=?, size=?, error=NULL WHERE id=?",
              (res["sha"], w, h, res["size"], fid))
    # exact duplicate (same bytes)
    r = c.execute("SELECT photo_id FROM files WHERE sha256=? AND photo_id IS NOT NULL AND id!=? LIMIT 1",
                  (res["sha"], fid)).fetchone()
    target = r[0] if r else None
    if target is None and hidx.ids:
        d = hidx.dist(res["phash"])
        for k in np.argsort(d):
            if d[k] > near:
                break
            if abs(hidx.aspect[k] - w / max(h, 1)) / max(hidx.aspect[k], 1e-6) < 0.03 \
                    and same_capture_time(hidx.ts[k], meta.get("taken_ts")):
                target = hidx.ids[int(k)]
                break
    if target is not None:
        _merge_duplicate(c, target, fid, res, meta)
        c.execute("UPDATE files SET status='done', photo_id=? WHERE id=?", (target, fid))
        return
    # burst group: similar photo taken a few seconds apart
    burst = None
    if hidx.ids and meta.get("taken_ts"):
        d = hidx.dist(res["phash"])
        for k in np.where(d <= burst_bits)[0]:
            t = hidx.ts[k]
            if t and abs(t - meta["taken_ts"]) <= burst_s:
                burst = hidx.burst[k] or hidx.ids[k]
                if hidx.burst[k] is None:
                    c.execute("UPDATE photos SET burst=? WHERE id=?", (burst, hidx.ids[k]))
                    hidx.burst[k] = burst
                break
    place = geo.lookup_many([(meta["lat"], meta["lon"])])[0] if meta.get("lat") is not None else {}
    pid = c.execute(
        """INSERT INTO photos(best_file,width,height,phash,sha256,taken_local,taken_ts,lat,lon,cc,country,admin,city,
           make,model,lens,focal,focal35,facing,orientation,burst,thumb,added) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, w, h, res["phash"], res["sha"], meta.get("taken_local"), meta.get("taken_ts"), meta.get("lat"),
         meta.get("lon"), place.get("cc"), place.get("country"), place.get("admin"), place.get("city"),
         meta.get("make"), meta.get("model"), meta.get("lens"), meta.get("focal"), meta.get("focal35"),
         meta.get("facing") or "unknown", meta.get("orientation"), burst, res["thumb"], db.now())).lastrowid
    c.execute("UPDATE files SET status='done', photo_id=? WHERE id=?", (pid, fid))
    hidx.add(pid, res["phash"], w, h, meta.get("taken_ts"), burst)
    _detect_faces(c, pid, res, det, arc, new_faces)


def same_capture_time(t1, t2) -> bool:
    """Copies of one photo have the same capture time. Burst shots differ by >= 1 s.
    A difference of whole 15-minute steps (up to 14 h) is a time zone difference between sources."""
    if t1 is None or t2 is None:
        return True
    dt = abs(t1 - t2)
    if dt > 14 * 3600 + 1:
        return False
    r = dt % 900
    return min(r, 900 - r) < 1.0


def _merge_duplicate(c, pid, fid, res, meta):
    row = c.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    sets, args = [], []
    for k in META_FIELDS:
        if row[k] is None and meta.get(k) is not None:
            sets.append(f"{k}=?")
            args.append(meta[k])
    if row["facing"] == "unknown" and meta.get("facing") not in (None, "unknown"):
        sets.append("facing=?")
        args.append(meta["facing"])
    if row["lat"] is None and meta.get("lat") is not None:
        place = geo.lookup_many([(meta["lat"], meta["lon"])])[0]
        for k in ("cc", "country", "admin", "city"):
            sets.append(f"{k}=?")
            args.append(place.get(k))
    old_area = (row["width"] or 0) * (row["height"] or 0)
    old_size = c.execute("SELECT size FROM files WHERE id=?", (row["best_file"],)).fetchone()
    better = res["width"] * res["height"] > old_area * 1.02 or (
        res["width"] * res["height"] >= old_area and old_size and res["size"] > (old_size[0] or 0) * 1.05)
    if better:
        sets += ["best_file=?", "width=?", "height=?", "thumb=?", "sha256=?", "stage2=0"]
        args += [fid, res["width"], res["height"], res["thumb"], res["sha"]]
    if sets:
        c.execute(f"UPDATE photos SET {', '.join(sets)} WHERE id=?", (*args, pid))


def _detect_faces(c, pid, res, det, arc, new_faces):
    from .models import align_face

    img = res["img"]
    H, W = img.shape[:2]
    t0 = time.perf_counter()
    boxes, scores, kps = det.detect(res["det"])
    _throttle(t0)
    if len(boxes):
        boxes = boxes / res["det_scale"]
        kps = kps / res["det_scale"]
    keep = [i for i in range(len(boxes)) if min(boxes[i, 2] - boxes[i, 0], boxes[i, 3] - boxes[i, 1]) >= 12]
    n_people = 0
    if keep:
        crops = [align_face(img, kps[i]) for i in keep]
        t0 = time.perf_counter()
        embs, norms = arc.embed(crops)
        _throttle(t0)
        pil = Image.fromarray(img)
        for j, i in enumerate(keep):
            b = np.clip(boxes[i] / [W, H, W, H], 0, 1)
            k = (kps[i] / [W, H]).round(5).tolist()
            size_px = float(boxes[i, 2] - boxes[i, 0])
            fid = c.execute("INSERT INTO faces(photo_id,x1,y1,x2,y2,det,kps,size_px,emb,emb_norm) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (pid, *map(float, b), float(scores[i]), json.dumps(k), size_px,
                             db.f32(embs[j]), float(norms[j]))).lastrowid
            new_faces.append(fid)
            imaging.save_jpeg(imaging.square_crop(pil, b, 1.6, 256), face_crop_path(fid), quality=85)
            if scores[i] >= 0.6 and (b[2] - b[0]) >= 0.03:
                n_people += 1
    c.execute("UPDATE photos SET n_faces=?, n_people=? WHERE id=?", (len(keep), n_people, pid))


# ---------------------------------------------------------------- pass 2

def pending_pass2():
    return db.q("""SELECT p.id, p.me_face, p.best_file FROM photos p WHERE p.me_face IS NOT NULL AND p.stage2=0
                   AND p.missing=0 ORDER BY p.id""")


def _readable_file(photo_id, best_file):
    rows = db.q("SELECT * FROM files WHERE photo_id=? AND status='done' ORDER BY (id=?) DESC, width*height DESC",
                (photo_id, best_file))
    for r in rows:
        p = file_path(r)
        if p is not None:
            return p, r
    return None, (rows[0] if rows else None)


def run_pass2(pool, prog: Progress, clip, iqa) -> int:
    todo = pending_pass2()
    if not todo:
        return 0
    jobs, wanted = [], []
    for pid, face_id, best in todo:
        path, frow = _readable_file(pid, best)
        if path is None:
            if frow is not None:
                wanted.append(frow["id"])
            continue
        f = db.q1("SELECT x1,y1,x2,y2,kps FROM faces WHERE id=?", (face_id,))
        rated = db.val("SELECT 1 FROM ratings WHERE face_id=? AND active=1", (face_id,))
        jobs.append({"photo_id": pid, "face_id": face_id, "path": str(path), "box": [f[0], f[1], f[2], f[3]],
                     "kps": json.loads(f[4]), "preview": None if rated else str(preview_path(pid))})
    if wanted:  # phone photos: ask the phone to upload them again (only photos of me)
        with db.tx() as c:
            c.executemany("UPDATE files SET want=MAX(want,2) WHERE id=?", [(i,) for i in wanted])
    if not jobs:
        return 0
    prog.set_phase("Analyzing my photos", len(jobs))
    from .workers import features_task

    buf = []
    n = 0
    for res in pool.imap(features_task, jobs):
        if not res.get("ok"):
            log.warning("features failed for photo %s: %s", res.get("photo_id"), res.get("error"))
            db.ex("UPDATE photos SET stage2=2 WHERE id=?", (res.get("photo_id"),))
            prog.step()
            continue
        buf.append(res)
        if len(buf) >= 8:
            n += _flush_pass2(buf, clip, iqa)
            prog.step(len(buf))
            buf = []
    if buf:
        n += _flush_pass2(buf, clip, iqa)
        prog.step(len(buf))
    enforce_preview_cap()
    return n


def _flush_pass2(buf, clip, iqa):
    t0 = time.perf_counter()
    ef = clip.embed([r["clip_face"] for r in buf])
    eh = clip.embed([r["clip_head"] for r in buf])
    src = beauty_source()
    beauty = beauty_from_clip(ef) if src == "mebeauty" else beauty_scores([r["face_id"] for r in buf])
    eF = clip.embed([r["clip_full"] for r in buf])
    q = iqa.score([r["iqa_in"] for r in buf])
    _throttle(t0)
    with db.tx() as c:
        for k, r in enumerate(buf):
            face = c.execute("SELECT det, emb_norm, sim FROM faces WHERE id=?", (r["face_id"],)).fetchone()
            ph = c.execute("SELECT facing FROM photos WHERE id=?", (r["photo_id"],)).fetchone()
            f = dict(r["features"])
            f.update(det=face[0], emb_norm=face[1], me_sim=face[2] if face[2] is not None else 1.0, iqa=q[k],
                     facing=ph[0])
            if beauty[k] is not None:
                f["beauty"], f["beauty_src"] = beauty[k], src
            c.execute("""INSERT INTO features(face_id,photo_id,data,clip_face,clip_full,iqa,version,updated,clip_head)
                         VALUES(?,?,?,?,?,?,1,?,?) ON CONFLICT(face_id) DO UPDATE SET data=excluded.data,
                         clip_face=excluded.clip_face, clip_full=excluded.clip_full, iqa=excluded.iqa,
                         updated=excluded.updated, clip_head=excluded.clip_head""",
                      (r["face_id"], r["photo_id"], json.dumps(f), db.f32(ef[k]), db.f32(eF[k]), q[k], db.now(),
                       db.f32(eh[k])))
            c.execute("UPDATE photos SET stage2=1 WHERE id=?", (r["photo_id"],))
            if r.get("preview_bytes"):
                c.execute("INSERT OR REPLACE INTO previews(photo_id,path,bytes,used) VALUES(?,?,?,?)",
                          (r["photo_id"], str(preview_path(r["photo_id"])), r["preview_bytes"], db.now()))
    return len(buf)


def beauty_source():
    """'mebeauty' (CLIP head trained at install), 'scut' (SCUT-FBP5500 ResNet-18), or None."""
    from . import beauty_train
    from .models import missing_models

    if beauty_train.head_path().exists():
        return "mebeauty"
    if "beauty" not in missing_models():
        return "scut"
    return None


def beauty_from_clip(clip_face_embs):
    """MEBeauty head: one dot product per face on the CLIP face embedding. Scale 1..10."""
    from . import beauty_train

    w, b = beauty_train.load_head()
    E = np.asarray(clip_face_embs, dtype=np.float32).reshape(-1, 512)
    return [float(np.clip(v, 1, 10)) for v in E @ w + b]


def beauty_scores(face_ids):
    """SCUT-FBP5500 model on the cached 256 px face crops, converted from its 1..5 scale to 1..10."""
    from .models import Beauty, missing_models

    if "beauty" in missing_models():
        return [None] * len(face_ids)
    crops, idx = [], []
    for i, fid in enumerate(face_ids):
        p = face_crop_path(fid)
        if p.exists():
            crops.append(np.asarray(Image.open(p).convert("RGB").resize((256, 256)), dtype=np.uint8))
            idx.append(i)
    out = [None] * len(face_ids)
    if crops:
        for i, v in zip(idx, Beauty().score(crops)):
            out[i] = 1 + (float(v) - 1) * 9 / 4
    return out


def backfill_beauty(prog: Progress | None = None) -> int:
    """(Re)compute the attractiveness value where it is missing or comes from another model (no photo is read again)."""
    src = beauty_source()
    if src is None:
        return 0
    rows = db.q("SELECT face_id, data, clip_face FROM features WHERE json_extract(data, '$.beauty') IS NULL "
                "OR COALESCE(json_extract(data, '$.beauty_src'), 'scut1to5') != ?", (src,))
    if not rows:
        return 0
    if prog:
        prog.set_phase("Updating the attractiveness values", len(rows))
    n = 0
    for i in range(0, len(rows), 256):
        chunk = rows[i:i + 256]
        if src == "mebeauty":
            vals = beauty_from_clip([np.frombuffer(r[2], np.float32) for r in chunk])
        else:
            vals = beauty_scores([r[0] for r in chunk])
        upd = []
        for (fid, data, _), v in zip(chunk, vals):
            if v is not None:
                d = json.loads(data)
                d["beauty"], d["beauty_src"] = v, src
                upd.append((json.dumps(d), fid))
        with db.tx() as c:
            c.executemany("UPDATE features SET data=? WHERE face_id=?", upd)
        n += len(upd)
        if prog:
            prog.step(len(chunk))
    return n


def enforce_preview_cap():
    cap = int(config.settings()["preview_cache_mb"]) * 1024 * 1024
    total = db.val("SELECT SUM(bytes) FROM previews", default=0)
    if total <= cap:
        return
    for pid, path, b in db.q("SELECT photo_id, path, bytes FROM previews ORDER BY used ASC"):
        drop_preview(pid, path)
        total -= b or 0
        if total <= cap * 0.9:
            break


def drop_preview(photo_id, path=None):
    path = path or db.val("SELECT path FROM previews WHERE photo_id=?", (photo_id,))
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
    db.ex("DELETE FROM previews WHERE photo_id=?", (photo_id,))


# ---------------------------------------------------------------- spool cleanup

def need_hair_update() -> int:
    """Photos analyzed before the hair measurements existed: analyze them again (the originals are read again)."""
    rows = db.q("SELECT photo_id FROM features WHERE json_extract(data, '$.hair_v') IS NULL")
    if rows:
        with db.tx() as c:
            c.executemany("UPDATE photos SET stage2=0 WHERE id=? AND stage2=1", [(r[0],) for r in rows])
    return len(rows)


def cleanup_spool():
    """Delete uploaded phone originals that are not needed any more."""
    rows = db.q("""SELECT f.id, f.source_id, f.status, p.me_face, p.stage2 FROM files f
                   JOIN sources s ON s.id=f.source_id LEFT JOIN photos p ON p.id=f.photo_id
                   WHERE s.kind='device'""")
    has_me = people.me_id() is not None
    for fid, sid, status, me_face, stage2 in rows:
        sp = spool_path(sid, fid)
        if not sp.exists() or status == "uploaded":
            continue
        need = has_me and me_face is not None and stage2 == 0
        if not need:
            try:
                sp.unlink()
            except OSError:
                pass
            db.ex("UPDATE files SET want=0 WHERE id=? AND want=2", (fid,))


# ---------------------------------------------------------------- the scan task

def scan_job(prog: Progress, list_sources: bool = True):
    global ACTIVE_POOL
    from . import ranking
    from .models import CLIP, IQA, SCRFD, ArcFace
    from .workers import Pool

    if list_sources:
        for src in db.q("SELECT * FROM sources WHERE kind='folder' AND enabled=1"):
            prog.set_phase(f"Listing files in {src['name']}")
            st = list_folder(src, prog)
            prog.message = (f"{src['name']}: {st['seen']} images, {st['new']} new, {st['changed']} changed, "
                            f"{st['missing']} missing, {st['videos']} videos skipped")
    pool = None
    new_faces: list[int] = []
    total_photos_before = db.val("SELECT COUNT(*) FROM photos", default=0)
    try:
        total = count_pass1()
        if total:
            pool = ACTIVE_POOL = Pool()
            det, arc = SCRFD(), ArcFace()
            hidx = HashIndex()
            prog.set_phase("Scanning photos", total)
            while True:
                rows = pending_pass1()
                if not rows:
                    break
                prog.total = max(prog.total, prog.done + count_pass1())
                run_pass1(rows, pool, prog, det, arc, hidx, new_faces)
                if new_faces and db.val("SELECT COUNT(*) FROM persons", default=0):
                    people.match(new_faces)
                cleanup_spool_partial = (people.me_id() is not None)
                if cleanup_spool_partial and pending_pass2():
                    run_pass2(pool, prog, CLIP(), IQA())
                    prog.set_phase("Scanning photos", prog.done + count_pass1())
                cleanup_spool()
        if people.me_id() is not None and pending_pass2():
            if pool is None:
                pool = ACTIVE_POOL = Pool()
            run_pass2(pool, prog, CLIP(), IQA())
        cleanup_spool()
    finally:
        ACTIVE_POOL = None
        if pool:
            pool.close()
    new_photos = db.val("SELECT COUNT(*) FROM photos", default=0) - total_photos_before
    if new_faces:
        prog.set_phase("Grouping faces")
        people.cluster(prog)
    infer_facing()
    backfill_beauty(prog)
    prog.set_phase("Scoring")
    ranking.score_all(prog)
    from . import search

    search.index_job(prog)
    prog.message = f"{new_photos} new photos, {len(new_faces)} new faces"


def infer_facing():
    """Photos with no lens info: copy front/rear from photos of the same phone with the same focal length."""
    rows = db.q("""SELECT model, ROUND(focal,2), facing, COUNT(*) FROM photos
                   WHERE model IS NOT NULL AND focal IS NOT NULL AND facing!='unknown' GROUP BY 1,2,3""")
    known = {}
    for model, focal, facing, n in rows:
        known.setdefault((model, focal), {})[facing] = n
    upd = []
    for (model, focal), d in known.items():
        if len(d) == 1:
            upd.append((next(iter(d)), model, focal))
    with db.tx() as c:
        c.executemany("UPDATE photos SET facing=? WHERE facing='unknown' AND model=? AND ROUND(focal,2)=?", upd)

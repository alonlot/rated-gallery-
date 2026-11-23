"""Identity: find me, other people, uncertain matches, clustering of unnamed faces.

All identity work uses ArcFace embeddings (cosine similarity). Names of other people are only
used as filters. Nothing here rates anybody.

faces.assign values:
  'ref'        the user picked this face as a reference for person_id
  'confirmed'  the user confirmed an uncertain match
  'auto'       similarity >= accept threshold
  'uncertain'  reject <= similarity < accept: shown to the user to confirm or reject
  NULL         no person
"""
from __future__ import annotations

import threading

import numpy as np

from . import config, db

MANUAL = ("ref", "confirmed")
ASSIGNED = ("ref", "confirmed", "auto")
MIN_CLUSTER_PX = 32
_cache = {"ver": None, "ids": None, "emb": None, "photo": None, "pos": None}
_lock = threading.Lock()


def _version():
    r = db.q1("SELECT COUNT(*), COALESCE(MAX(id),0) FROM faces")
    return (r[0], r[1])


def face_matrix():
    """(ids, embeddings, photo_ids) of all faces, cached until faces change."""
    with _lock:
        v = _version()
        if _cache["ver"] != v:
            rows = db.q("SELECT id, photo_id, emb FROM faces WHERE emb IS NOT NULL ORDER BY id")
            ids = np.array([r[0] for r in rows], dtype=np.int64)
            photo = np.array([r[1] for r in rows], dtype=np.int64)
            emb = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(-1, 512) if rows \
                else np.zeros((0, 512), np.float32)
            _cache.update(ver=v, ids=ids, emb=emb, photo=photo, pos={int(i): k for k, i in enumerate(ids)})
        return _cache["ids"], _cache["emb"], _cache["photo"], _cache["pos"]


def me_id() -> int | None:
    return db.val("SELECT id FROM persons WHERE is_me=1 ORDER BY id LIMIT 1")


def _person_sets():
    out = {}
    ids, emb, _, pos = face_matrix()
    for p in db.q("SELECT id FROM persons"):
        pid = p[0]
        prow = [r[0] for r in db.q(f"SELECT id FROM faces WHERE person_id=? AND assign IN {MANUAL}", (pid,))]
        nrow = [r[0] for r in db.q("SELECT face_id FROM face_rejects WHERE person_id=?", (pid,))]
        P = np.array([pos[i] for i in prow if i in pos], dtype=np.int64)
        N = np.array([pos[i] for i in nrow if i in pos], dtype=np.int64)
        if len(P) > 400:  # keep the matrix small; references first
            refs = {r[0] for r in db.q("SELECT id FROM faces WHERE person_id=? AND assign='ref'", (pid,))}
            keep = [k for k, i in zip(P, prow) if i in refs][:200]
            rest = [k for k, i in zip(P, prow) if i not in refs]
            rng = np.random.default_rng(pid)
            keep += list(rng.choice(rest, min(len(rest), 400 - len(keep)), replace=False)) if rest else []
            P = np.array(keep, dtype=np.int64)
        out[pid] = (emb[P] if len(P) else None, emb[N] if len(N) else None)
    return out


def _scores(E, Pm, Nm):
    S = E @ Pm.T
    k = min(3, S.shape[1])
    s_pos = np.sort(S, axis=1)[:, -k:].mean(1) if k > 1 else S[:, 0]
    s_pos = np.maximum(s_pos, S.max(1) - 0.08)  # one very close reference still counts
    s_neg = (E @ Nm.T).max(1) if Nm is not None else np.full(len(E), -1.0)
    return s_pos, s_neg


def match(face_ids=None):
    """Recompute automatic assignments. face_ids=None means all faces. Manual answers stay."""
    ids, emb, photo, pos = face_matrix()
    if not len(ids):
        return 0
    s = config.settings()
    acc, rej = float(s["match_accept"]), float(s["match_reject"])
    sets = _person_sets()
    if face_ids is None:
        sel = np.arange(len(ids))
    else:
        sel = np.array([pos[i] for i in face_ids if i in pos], dtype=np.int64)
    if not len(sel):
        return 0
    manual = {r[0] for r in db.q(f"SELECT id FROM faces WHERE assign IN {MANUAL}")}
    rejects = {}
    for r in db.q("SELECT face_id, person_id FROM face_rejects"):
        rejects.setdefault(r[0], set()).add(r[1])
    E = emb[sel]
    best_p = np.full(len(sel), -1, dtype=np.int64)
    best_s = np.full(len(sel), -1.0)
    for pid, (Pm, Nm) in sets.items():
        if Pm is None:
            continue
        sp, sn = _scores(E, Pm, Nm)
        ok = (sp > best_s) & (sp >= rej) & (sp > sn)
        best_p[ok], best_s[ok] = pid, sp[ok]
    updates = []
    for k, idx in enumerate(sel):
        fid = int(ids[idx])
        if fid in manual:
            continue
        pid, sc = int(best_p[k]), float(best_s[k])
        if pid >= 0 and pid in rejects.get(fid, ()):
            pid = -1
        if pid < 0:
            updates.append((None, None, sc if sc > -1 else None, fid))
        elif sc >= acc:
            updates.append((pid, "auto", sc, fid))
        else:
            updates.append((pid, "uncertain", sc, fid))
    with db.tx() as c:
        c.executemany("UPDATE faces SET person_id=?, assign=?, sim=? WHERE id=?", updates)
    update_me_faces(None if face_ids is None else {int(photo[i]) for i in sel})
    return len(updates)


def update_me_faces(photo_ids=None):
    """photos.me_face = my clearest face in the photo (by similarity and size)."""
    mid = me_id()
    where = "" if photo_ids is None else f" AND photo_id IN ({','.join(str(int(p)) for p in photo_ids) or '0'})"
    rows = db.q(f"SELECT id, photo_id, sim, size_px FROM faces WHERE person_id=? AND assign IN {ASSIGNED}{where}",
                (mid if mid else -1,))
    best = {}
    for fid, pid, sim, size in rows:
        sc = (sim if sim is not None else 1.0) * np.sqrt(max(size or 1, 1))
        if pid not in best or sc > best[pid][1]:
            best[pid] = (fid, sc)
    with db.tx() as c:
        if photo_ids is None:
            c.execute("UPDATE photos SET me_face=NULL")
        else:
            c.executemany("UPDATE photos SET me_face=NULL WHERE id=?", [(int(p),) for p in photo_ids])
        c.executemany("UPDATE photos SET me_face=? WHERE id=?", [(f, p) for p, (f, _) in best.items()])
        # features of a face that is not "my face" any more are stale for scoring
        c.execute("UPDATE photos SET stage2=0 WHERE me_face IS NOT NULL AND stage2=1 "
                  "AND me_face NOT IN (SELECT face_id FROM features)")


# ---------------------------------------------------------------- user actions

def create_person(name: str, face_ids, is_me=False) -> int:
    with db.tx() as c:
        if is_me:
            existing = c.execute("SELECT id FROM persons WHERE is_me=1").fetchone()
            if existing:
                pid = existing[0]
            else:
                pid = c.execute("INSERT INTO persons(name,is_me,created) VALUES(?,1,?)", (name or "Me", db.now())).lastrowid
        else:
            pid = c.execute("INSERT INTO persons(name,is_me,created) VALUES(?,0,?)", (name, db.now())).lastrowid
    add_faces(pid, face_ids, "ref")
    return pid


def add_faces(pid: int, face_ids, kind="ref"):
    face_ids = [int(f) for f in face_ids]
    with db.tx() as c:
        c.executemany("UPDATE faces SET person_id=?, assign=?, sim=1.0, cluster=NULL WHERE id=?",
                      [(pid, kind, f) for f in face_ids])
        c.executemany("DELETE FROM face_rejects WHERE face_id=? AND person_id=?", [(f, pid) for f in face_ids])
    match()


def reject_faces(pid: int, face_ids):
    face_ids = [int(f) for f in face_ids]
    with db.tx() as c:
        c.executemany("INSERT OR IGNORE INTO face_rejects(face_id, person_id) VALUES(?,?)", [(f, pid) for f in face_ids])
        c.executemany("UPDATE faces SET person_id=NULL, assign=NULL WHERE id=? AND person_id=?", [(f, pid) for f in face_ids])
    match()


def rename_person(pid: int, name: str):
    db.ex("UPDATE persons SET name=? WHERE id=?", (name, pid))


def delete_person(pid: int):
    with db.tx() as c:
        c.execute("UPDATE faces SET person_id=NULL, assign=NULL WHERE person_id=?", (pid,))
        c.execute("DELETE FROM face_rejects WHERE person_id=?", (pid,))
        c.execute("DELETE FROM persons WHERE id=?", (pid,))
    match()


def uncertain(pid: int, limit=24):
    return db.q("SELECT id, photo_id, sim FROM faces WHERE person_id=? AND assign='uncertain' "
                "ORDER BY sim DESC LIMIT ?", (pid, limit))


def persons():
    rows = db.q(f"""SELECT p.id, p.name, p.is_me,
        (SELECT COUNT(*) FROM faces f WHERE f.person_id=p.id AND f.assign IN {ASSIGNED}) AS n,
        (SELECT COUNT(*) FROM faces f WHERE f.person_id=p.id AND f.assign IN {MANUAL}) AS nref,
        (SELECT COUNT(*) FROM faces f WHERE f.person_id=p.id AND f.assign='uncertain') AS nunc,
        (SELECT f.id FROM faces f WHERE f.person_id=p.id AND f.assign IN {MANUAL} ORDER BY f.size_px DESC LIMIT 1) AS cover
        FROM persons p ORDER BY p.is_me DESC, n DESC""")
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- clustering of unnamed faces

def cluster(progress=None, k=10, edge=0.42, iters=12):
    """Chinese-whispers clustering on a kNN graph of unnamed, clear faces."""
    ids, emb, _, pos = face_matrix()
    rows = db.q("SELECT id FROM faces WHERE (person_id IS NULL OR assign='uncertain') AND det>=0.6 AND size_px>=?",
                (MIN_CLUSTER_PX,))
    sel = np.array([pos[r[0]] for r in rows if r[0] in pos], dtype=np.int64)
    db.ex("UPDATE faces SET cluster=NULL")
    n = len(sel)
    if n < 2:
        return 0
    E = emb[sel]
    kk = min(k, n - 1)
    nbr = np.zeros((n, kk), dtype=np.int64)
    wts = np.zeros((n, kk), dtype=np.float32)
    if progress:
        progress.set_phase("Grouping faces", n)
    for s in range(0, n, 2048):
        S = E[s:s + 2048] @ E.T
        S[np.arange(S.shape[0]), np.arange(s, s + S.shape[0])] = -1
        top = np.argpartition(-S, kk - 1, axis=1)[:, :kk] if kk < n - 1 else np.argsort(-S, axis=1)[:, :kk]
        nbr[s:s + 2048] = top
        wts[s:s + 2048] = np.take_along_axis(S, top, 1)
        if progress:
            progress.step(S.shape[0])
    wts[wts < edge] = 0
    labels = np.arange(n)
    rng = np.random.default_rng(0)
    for _ in range(iters):
        changed = 0
        for i in rng.permutation(n):
            w = wts[i]
            if not w.any():
                continue
            acc = {}
            for j, ww in zip(nbr[i], w):
                if ww > 0:
                    acc[labels[j]] = acc.get(labels[j], 0.0) + ww
            lab = max(acc, key=acc.get)
            if lab != labels[i]:
                labels[i] = lab
                changed += 1
        if not changed:
            break
    uniq, counts = np.unique(labels, return_counts=True)
    big = {u: c for u, c in zip(uniq, counts) if c >= 2}
    order = sorted(big, key=lambda u: -big[u])
    cid = {u: i + 1 for i, u in enumerate(order)}
    updates = [(cid[labels[t]], int(ids[sel[t]])) for t in range(n) if labels[t] in cid]
    with db.tx() as c:
        c.executemany("UPDATE faces SET cluster=? WHERE id=?", updates)
    return len(cid)


UNNAMED = "(person_id IS NULL OR assign NOT IN ('ref','confirmed','auto'))"


def clusters(limit=60, sample=8):
    """Groups of faces that have no person yet (faces of named people are left out)."""
    rows = db.q(f"SELECT cluster, COUNT(*) n FROM faces WHERE cluster IS NOT NULL AND {UNNAMED} "
                "GROUP BY cluster HAVING n >= 2 ORDER BY n DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        faces = [x[0] for x in db.q(f"SELECT id FROM faces WHERE cluster=? AND {UNNAMED} ORDER BY det*size_px DESC LIMIT ?",
                                    (r[0], sample))]
        out.append({"id": r[0], "n": r[1], "faces": faces})
    return out


def cluster_faces(cid: int, limit=500):
    return [r[0] for r in db.q(f"SELECT id FROM faces WHERE cluster=? AND {UNNAMED} ORDER BY det*size_px DESC LIMIT ?",
                               (cid, limit))]


def merge_clusters(cids):
    cids = [int(c) for c in cids]
    if len(cids) < 2:
        return
    db.ex(f"UPDATE faces SET cluster=? WHERE cluster IN ({','.join('?' * len(cids))})", [cids[0], *cids])


def split_cluster(face_ids):
    new = (db.val("SELECT MAX(cluster) FROM faces", default=0) or 0) + 1
    with db.tx() as c:
        c.executemany("UPDATE faces SET cluster=? WHERE id=?", [(new, int(f)) for f in face_ids])
    return new


def unclustered_faces(limit=200):
    """Large faces with no person and no cluster (for picking references when clusters miss someone)."""
    return [r[0] for r in db.q("SELECT id FROM faces WHERE person_id IS NULL AND cluster IS NULL AND det>=0.6 "
                               "ORDER BY size_px DESC LIMIT ?", (limit,))]

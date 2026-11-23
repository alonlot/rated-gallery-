"""Personal score: prior heuristic, ridge model with pairwise constraints, scoring, active learning.

The score is only about me: only photos where my face is (photos.me_face) are scored.
"""
from __future__ import annotations

import json
import logging
import pickle
import time

import numpy as np

from . import config, db

log = logging.getLogger("ratedgallery.ranking")
LAYOUT = 2  # change when the feature layout changes; old models are then ignored

HAND = [
    ("eye_open", "Eyes open"), ("eye_asym", "One eye more closed"), ("smile", "Smile"),
    ("smile2", "Very big smile"), ("jaw_open", "Mouth open"), ("brow_up", "Brows raised"),
    ("brow_down", "Brows lowered"), ("squint", "Squinting"), ("frown", "Mouth corners down"),
    ("yaw_abs", "Head turned to the side"), ("pitch", "Head tilted up"), ("pitch_abs", "Head tilted up or down"),
    ("roll_abs", "Head tilted sideways"), ("sharp", "Sharp face"), ("face_mean", "Bright face"),
    ("face_mean_dev", "Face badly exposed"), ("face_std", "Contrast on face"), ("clip_hi", "Blown highlights on face"),
    ("clip_lo", "Black shadows on face"), ("bg_contrast", "Face brighter than background"),
    ("face_size", "Face large in frame"), ("front", "Front camera (selfie)"), ("det", "Face clearly visible"),
    ("emb_norm", "Face detail (identity model)"), ("me_sim", "Looks like my references"),
    ("iqa", "Image quality (TOPIQ)"), ("mp_ok", "Face landmarks found"),
    ("beauty", "General attractiveness model"),
]
HAND_KEYS = [k for k, _ in HAND]
BEAUTY_COL = HAND_KEYS.index("beauty")
BINARY_LABELS = {"front": ("Front camera (selfie)", "Rear camera"), "mp_ok": ("Face landmarks found", "Face landmarks not found")}
MIN_GAIN = 0.01  # the attractiveness model is used only if it improves held-out Spearman by this much
LABEL = dict(HAND)
N_PCA_FACE, N_PCA_FULL = 16, 8
PRIOR_W = {"eye_open": 1.0, "eye_asym": -0.5, "smile": 0.5, "smile2": -0.2, "sharp": 0.8, "face_mean_dev": -0.6,
           "clip_hi": -0.4, "clip_lo": -0.3, "yaw_abs": -0.7, "pitch_abs": -0.4, "roll_abs": -0.2, "iqa": 0.5,
           "face_size": 0.2, "squint": -0.2, "brow_down": -0.2, "det": 0.2}


def hand_vector(f: dict) -> list:
    g = lambda k: f.get(k, float("nan"))  # noqa: E731
    bl, br = g("eyeBlinkLeft"), g("eyeBlinkRight")
    smile = (g("mouthSmileLeft") + g("mouthSmileRight")) / 2
    return [1 - (bl + br) / 2, abs(bl - br), smile, smile * smile, g("jawOpen"),
            (g("browInnerUp") + (g("browOuterUpLeft") + g("browOuterUpRight")) / 2) / 2,
            (g("browDownLeft") + g("browDownRight")) / 2, (g("eyeSquintLeft") + g("eyeSquintRight")) / 2,
            (g("mouthFrownLeft") + g("mouthFrownRight")) / 2,
            abs(f.get("yaw", 0.0)), f.get("pitch", 0.0), abs(f.get("pitch", 0.0)), abs(f.get("roll", 0.0)),
            f.get("sharp", 0.0), f.get("face_mean", 0.5), abs(f.get("face_mean", 0.5) - 0.5), f.get("face_std", 0.0),
            f.get("clip_hi", 0.0), f.get("clip_lo", 0.0), f.get("bg_contrast", 0.0),
            float(np.log(max(f.get("face_frac", 1e-4), 1e-5))),
            {"front": 1.0, "rear": 0.0}.get(f.get("facing"), 0.5), f.get("det", 0.8), f.get("emb_norm", 20.0),
            f.get("me_sim", 0.5), f.get("iqa", 0.5), f.get("mp_ok", 1.0), f.get("beauty", float("nan"))]


# ---------------------------------------------------------------- library

class Library:
    """All my primary faces that have features."""

    def __init__(self):
        rows = db.q("""SELECT f.face_id, f.photo_id, f.data, f.clip_face, f.clip_full, p.burst
                       FROM features f JOIN photos p ON p.me_face=f.face_id AND p.id=f.photo_id
                       WHERE p.missing=0 AND p.stage2=1 ORDER BY f.face_id""")
        self.face = np.array([r[0] for r in rows], dtype=np.int64)
        self.photo = np.array([r[1] for r in rows], dtype=np.int64)
        self.burst = [r[5] for r in rows]
        self.feat = [json.loads(r[2]) for r in rows]
        self.H = np.array([hand_vector(f) for f in self.feat], dtype=np.float64).reshape(-1, len(HAND))
        self.CF = np.frombuffer(b"".join(r[3] for r in rows), np.float32).reshape(-1, 512).astype(np.float64) if rows else np.zeros((0, 512))
        self.CA = np.frombuffer(b"".join(r[4] for r in rows), np.float32).reshape(-1, 512).astype(np.float64) if rows else np.zeros((0, 512))
        self.pos = {int(f): i for i, f in enumerate(self.face)}

    def __len__(self):
        return len(self.face)


def _pca(X, k, seed=0):
    mu = X.mean(0)
    Xc = X - mu
    if len(X) > 20000:
        Xc_fit = Xc[np.random.default_rng(seed).choice(len(X), 20000, replace=False)]
    else:
        Xc_fit = Xc
    k = max(1, min(k, len(X) - 1, X.shape[1]))
    _, _, Vt = np.linalg.svd(Xc_fit, full_matrices=False)
    comp = Vt[:k]
    Z = Xc @ comp.T
    sd = Z.std(0) + 1e-9
    return {"mu": mu, "comp": comp, "sd": sd}


def _pca_apply(p, X):
    return (X - p["mu"]) @ p["comp"].T / p["sd"]


def fit_transform(lib: Library):
    H = lib.H.copy()
    col_mean = np.nanmean(H, 0) if len(H) else np.zeros(H.shape[1])
    col_mean = np.where(np.isnan(col_mean), 0, col_mean)
    tf = {"fill": col_mean}
    H = np.where(np.isnan(H), col_mean, H)
    tf["mu"], tf["sd"] = H.mean(0), H.std(0) + 1e-6
    tf["pf"] = _pca(lib.CF, N_PCA_FACE) if len(lib) >= 4 else None
    tf["pa"] = _pca(lib.CA, N_PCA_FULL) if len(lib) >= 4 else None
    return tf, transform(tf, lib)


def transform(tf, lib: Library):
    H = np.where(np.isnan(lib.H), tf["fill"], lib.H)
    parts = [(H - tf["mu"]) / tf["sd"]]
    if tf.get("pf") is not None:
        parts.append(_pca_apply(tf["pf"], lib.CF))
        parts.append(_pca_apply(tf["pa"], lib.CA))
    return np.hstack(parts)


def feature_groups(tf):
    names = [LABEL[k] for k in HAND_KEYS]
    if tf.get("pf") is not None:
        names += ["Overall look of my face (learned)"] * len(tf["pf"]["comp"])
        names += ["Scene and style of the photo (learned)"] * len(tf["pa"]["comp"])
    return names


# ---------------------------------------------------------------- labels

def current_ratings():
    """The newest active rating of each face (by the time it was made; phone ratings can arrive later)."""
    return {r[0]: r[1] for r in db.q("""SELECT r.face_id, r.rating FROM ratings r WHERE r.active=1 AND NOT EXISTS
              (SELECT 1 FROM ratings r2 WHERE r2.face_id=r.face_id AND r2.active=1
               AND (r2.created > r.created OR (r2.created = r.created AND r2.id > r.id)))""")}


def current_pairs():
    return [(r[0], r[1], r[2]) for r in db.q("SELECT face_a, face_b, result FROM comparisons WHERE active=1")]


def label_counts():
    return len(current_ratings()), len(current_pairs())


# ---------------------------------------------------------------- model

def _solve(X, y, ridx, pairs, alpha, pw=1.0, margin=1.5):
    d = X.shape[1]
    A, b = [], []
    if len(ridx):
        A.append(np.hstack([X[ridx], np.ones((len(ridx), 1))]))
        b.append(np.asarray(y, float))
    if pairs:
        ia = np.array([p[0] for p in pairs]); ib = np.array([p[1] for p in pairs]); t = np.array([p[2] for p in pairs], float)
        A.append(np.hstack([X[ia] - X[ib], np.zeros((len(pairs), 1))]) * np.sqrt(pw))
        b.append(margin * t * np.sqrt(pw))
    A = np.vstack(A); b = np.concatenate(b)
    reg = np.eye(d + 1) * alpha
    reg[d, d] = 1e-6
    w = np.linalg.solve(A.T @ A + reg, A.T @ b)
    if not len(ridx):
        w[d] = 5.5
    return w[:d], w[d]


def _spearman(a, b):
    from scipy.stats import spearmanr

    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return None
    return float(spearmanr(a, b).statistic)


ALPHAS = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0)


def _cv(X, ridx, y, pairs, alpha_choices, folds=5, repeats=3, seed=0, inner=True):
    """Out-of-fold predictions for rated items. With inner=True, alpha is chosen inside each fold."""
    n = len(ridx)
    oof = np.zeros(n)
    cnt = np.zeros(n)
    rng = np.random.default_rng(seed)
    for _ in range(repeats):
        perm = rng.permutation(n)
        for f in range(min(folds, n)):
            test = perm[f::min(folds, n)]
            train = np.setdiff1d(perm, test)
            test_items = set(ridx[test].tolist())
            tp = [p for p in pairs if p[0] not in test_items and p[1] not in test_items]
            if inner and len(alpha_choices) > 1 and len(train) >= 8:
                a = _best_alpha(X, ridx[train], y[train], tp, alpha_choices, seed + f)
            else:
                a = alpha_choices[0]
            w, b = _solve(X, y[train], ridx[train], tp, a)
            oof[test] += X[ridx[test]] @ w + b
            cnt[test] += 1
    return oof / np.maximum(cnt, 1)


def _best_alpha(X, ridx, y, pairs, choices, seed=0):
    best, best_s = choices[len(choices) // 2], -2
    for a in choices:
        o = _cv(X, ridx, y, pairs, (a,), folds=4, repeats=1, seed=seed, inner=False)
        s = _spearman(o, y)
        if s is not None and s > best_s:
            best, best_s = a, s
    return best


def train(prog=None):
    lib = Library()
    ratings = current_ratings()
    pairs_raw = current_pairs()
    ridx = np.array([lib.pos[f] for f in ratings if f in lib.pos], dtype=np.int64)
    y = np.array([ratings[int(lib.face[i])] for i in ridx], float)
    pairs = []
    for a, b, r in pairs_raw:
        if a in lib.pos and b in lib.pos:
            pairs.append((lib.pos[a], lib.pos[b], {"a": 1, "b": -1, "eq": 0}.get(r, 0)))
    if len(ridx) < 8:
        return None
    tf, X = fit_transform(lib)
    if prog:
        prog.set_phase("Training my model", 4)
    full = np.ones(X.shape[1], dtype=bool)
    nob = full.copy()
    nob[BEAUTY_COL] = False
    sp_with = sp_without = oof_with = oof_without = None
    if len(ridx) >= 10:
        # same folds (same seed) for both, so the comparison is fair
        oof_without = _cv(X[:, nob], ridx, y, pairs, ALPHAS, inner=True)
        sp_without = _spearman(oof_without, y)
        if prog:
            prog.step()
        oof_with = _cv(X, ridx, y, pairs, ALPHAS, inner=True)
        sp_with = _spearman(oof_with, y)
    if prog:
        prog.step()
    use_beauty = sp_with is not None and sp_without is not None and sp_with >= sp_without + MIN_GAIN
    mask = full if use_beauty else nob
    oof, sp = (oof_with, sp_with) if use_beauty else (oof_without, sp_without)
    Xm = X[:, mask]
    alpha = _best_alpha(Xm, ridx, y, pairs, ALPHAS)
    if prog:
        prog.step()
    wm, b = _solve(Xm, y, ridx, pairs, alpha)
    w = np.zeros(X.shape[1])
    w[mask] = wm
    rng = np.random.default_rng(1)
    boot = []
    for _ in range(20):
        s_ = rng.choice(len(ridx), len(ridx), replace=True)
        bp = [pairs[i] for i in rng.choice(len(pairs), len(pairs), replace=True)] if pairs else []
        bw, bb = _solve(Xm, y[s_], ridx[s_], bp, alpha)
        full_w = np.zeros(X.shape[1])
        full_w[mask] = bw
        boot.append((full_w, bb))
    raw = X @ w + b
    m = {"layout": LAYOUT, "tf": tf, "w": w, "b": b, "alpha": alpha, "boot_w": np.array([x[0] for x in boot]),
         "boot_b": np.array([x[1] for x in boot]), "ratings_sorted": np.sort(y), "raw_sorted": np.sort(raw),
         "spearman": sp, "n_labels": len(ridx), "n_pairs": len(pairs),
         "beauty": {"used": bool(use_beauty), "with": sp_with, "without": sp_without}}
    if oof is not None:
        m["oof"] = [(int(lib.face[i]), float(yy), float(o)) for i, yy, o in zip(ridx, y, oof)]
    with db.tx() as c:
        c.execute("UPDATE models SET current=0")
        c.execute("INSERT INTO models(created,n_labels,n_pairs,spearman,blob,current) VALUES(?,?,?,?,?,1)",
                  (db.now(), len(ratings), len(pairs_raw), sp, pickle.dumps(m)))
    if prog:
        prog.step()
    log.info("Trained: %d ratings, %d pairs, alpha=%s, spearman=%s", len(ridx), len(pairs), alpha, sp)
    return m


def current_model():
    r = db.q1("SELECT id, blob FROM models WHERE current=1 ORDER BY id DESC LIMIT 1")
    if not r:
        return None, None
    try:
        m = pickle.loads(r[1])
    except Exception:  # noqa: BLE001
        return None, None
    return (r[0], m) if m.get("layout") == LAYOUT else (None, None)


def pct_to_score(p, ratings_sorted):
    """Percentile 0..1 -> 1..10 with the distribution of my ratings (each whole rating spread over its unit)."""
    u, cnt = np.unique(np.asarray(ratings_sorted, float), return_counts=True)
    hi = np.cumsum(cnt) / cnt.sum()
    lo = np.concatenate([[0.0], hi[:-1]])
    j = np.clip(np.searchsorted(hi, p, side="left"), 0, len(u) - 1)
    frac = (p - lo[j]) / np.maximum(hi[j] - lo[j], 1e-12)
    return np.clip(u[j] - 0.5 + np.clip(frac, 0, 1), 1, 10)


PERIOD_MIN = 30


def period_scores(raw, years, ratings_sorted=None):
    """Score of each photo compared only with photos from about the same years.

    The window is the same year, then +-1, +-2, +-3 years, until it has PERIOD_MIN photos.
    Photos with no date are compared with all photos."""
    raw = np.asarray(raw, float)
    years = np.asarray(years)
    out = np.empty(len(raw))
    dated = years >= 0
    for y in np.unique(years):
        sel = years == y
        if y < 0:
            ref = raw
        else:
            for k in range(4):
                ref = raw[dated & (np.abs(years - y) <= k)]
                if len(ref) >= PERIOD_MIN:
                    break
        ref = np.sort(ref)
        p = np.interp(raw[sel], ref, np.linspace(0, 1, len(ref))) if len(ref) > 1 else np.full(sel.sum(), 0.5)
        out[sel] = pct_to_score(p, ratings_sorted) if ratings_sorted is not None else 1 + 9 * p
    return out


def to_score(m, raw):
    """Map raw model output to 1..10 so that the library has the distribution of my own ratings."""
    rs = m["raw_sorted"]
    raw = np.asarray(raw, dtype=float)
    # percentile of each raw value in the library (interpolated, so equal raw values are rare)
    p = np.interp(raw, rs, np.linspace(0, 1, len(rs))) if len(rs) > 1 else np.full(raw.shape, 0.5)
    # quantile of my ratings, with each whole rating r spread over [r-0.5, r+0.5] (no plateaus)
    return pct_to_score(p, m["ratings_sorted"])


def prior_raw(Xh_std):
    w = np.array([PRIOR_W.get(k, 0.0) for k in HAND_KEYS])
    return Xh_std @ w, w


def quality_conf(f: dict):
    face_w = f.get("face_w", 0.05)
    q = (np.clip(face_w / 0.10, 0.1, 1) ** 0.75
         * np.clip((f.get("sharp", 3.0) - 1.5) / 2.5, 0.15, 1)
         * np.clip(1 - (abs(f.get("yaw", 0.0)) - 30) / 40, 0.15, 1)
         * np.clip((f.get("det", 0.8) - 0.45) / 0.3, 0.2, 1)
         * (1.0 if f.get("mp_ok", 1.0) else 0.6))
    return float(q)


def score_all(prog=None):
    lib = Library()
    if not len(lib):
        db.ex("DELETE FROM scores")
        return 0
    mid, m = current_model()
    ratings = current_ratings()
    if m is not None:
        tf = m["tf"]
        X = transform(tf, lib)
        raw = X @ m["w"] + m["b"]
        boot = X @ m["boot_w"].T + m["boot_b"]
        scores = to_score(m, raw)
        bscores = to_score(m, boot.reshape(-1)).reshape(boot.shape)
        unc = bscores.std(1)
        sp = m.get("spearman") or 0.0
        mfac = np.clip(1 - unc / 2.5, 0.3, 1) * (0.5 + 0.5 * np.clip(sp, 0, 1))
        w = m["w"]
        names = feature_groups(tf)
        k = (np.std(scores) / (np.std(raw) + 1e-9))
        prior = 0
    else:
        tf, X = fit_transform(lib)
        Xh = X[:, :len(HAND)]
        raw, wp = prior_raw(Xh)
        order = raw.argsort().argsort()
        scores = 1 + 9 * order / max(len(raw) - 1, 1)
        mfac = np.full(len(raw), 0.3)
        w = np.concatenate([wp, np.zeros(X.shape[1] - len(HAND))])
        names = feature_groups(tf)
        k = (np.std(scores) / (np.std(raw) + 1e-9))
        prior = 1
    yr = {r[0]: int(r[1][:4]) if r[1] else -1 for r in
          db.q(f"SELECT id, taken_local FROM photos WHERE id IN ({','.join(map(str, lib.photo.tolist())) or '0'})")}
    years = np.array([yr.get(int(pid), -1) for pid in lib.photo])
    sp = period_scores(raw, years, m["ratings_sorted"] if m is not None else None)
    contrib = X * w * k
    nh = len(HAND)
    rows = []
    for i in range(len(lib)):
        agg = {}
        for j, (nm, v) in enumerate(zip(names, contrib[i])):
            key = HAND_KEYS[j] if j < nh else None
            if key in BINARY_LABELS:
                nm = BINARY_LABELS[key][0 if X[i, j] > 0 else 1]
            elif j < nh and abs(X[i, j]) >= 0.25:
                # say which way this photo differs from my average photo, so "+0.5" is not misread
                nm = f"{nm}: {'more' if X[i, j] > 0 else 'less'} than usual"
            agg[nm] = agg.get(nm, 0.0) + float(v)
        top = sorted(agg.items(), key=lambda t: -abs(t[1]))
        top = [[a, round(b, 2)] for a, b in top if abs(b) >= 0.05][:8]
        conf = quality_conf(lib.feat[i]) * (0.6 + 0.4 * float(mfac[i]))  # face quality first, model doubt second
        rows.append((int(lib.face[i]), int(lib.photo[i]), float(raw[i]), float(scores[i]), round(conf, 3),
                     prior, mid, json.dumps(top), float(sp[i])))
    with db.tx() as c:
        c.execute("DELETE FROM scores")
        c.executemany("INSERT INTO scores(face_id,photo_id,raw,score,conf,prior,model_id,contrib,score_p) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def train_job(prog):
    from .pipeline import backfill_beauty

    backfill_beauty(prog)
    m = train(prog)
    prog.set_phase("Scoring all my photos")
    n = score_all(prog)
    prog.message = (f"Model: {m['n_labels']} ratings, {m['n_pairs']} comparisons, "
                    f"Spearman {m['spearman']:.2f}" if m and m.get("spearman") is not None else "Not enough ratings yet") \
        + f"; {n} photos scored"


def labels_since_model():
    r = db.q1("SELECT n_labels, n_pairs FROM models WHERE current=1 ORDER BY id DESC LIMIT 1")
    nr, npairs = label_counts()
    if r is None:
        return nr + npairs
    return max(0, (nr + npairs) - (r[0] + r[1]))


def quality():
    nr, npairs = label_counts()
    _, m = current_model()
    sp = m.get("spearman") if m else None
    if nr < 10:
        msg = f"Rate {10 - nr} more photos to train your first model."
        level = "none"
    elif sp is None:
        msg = "Not enough ratings to measure the quality. Rate 10 more."
        level = "low"
    elif sp < 0.3:
        msg, level = "Model quality is low. Rate 20 more for better accuracy.", "low"
    elif sp < 0.5:
        msg, level = "Model quality is fair. Rate 20 more for better accuracy.", "fair"
    elif sp < 0.7:
        msg, level = "Model quality is good. More ratings still help.", "good"
    else:
        msg, level = "Model quality is very good.", "very good"
    b = (m or {}).get("beauty")
    if b and b.get("with") is not None and b.get("without") is not None:
        b = dict(b, message=(f"General attractiveness model: used. Spearman {b['with']:.2f} with it, {b['without']:.2f} without it."
                             if b["used"] else
                             f"General attractiveness model: not used, because it does not improve your model "
                             f"(Spearman {b['with']:.2f} with it, {b['without']:.2f} without it)."))
    elif m is not None:
        b = {"used": False, "message": "General attractiveness model: not used yet. It is tested when you have 10 or more ratings."}
    return {"ratings": nr, "pairs": npairs, "spearman": sp, "message": msg, "level": level, "beauty": b,
            "trained": bool(m), "library": db.val("SELECT COUNT(*) FROM scores", default=0)}


def surprising(limit=20):
    _, m = current_model()
    if not m or "oof" not in m:
        return []
    fids = [o[0] for o in m["oof"]]
    raw = np.array([o[2] for o in m["oof"]])
    pred = to_score(m, raw)
    now = current_ratings()
    out = []
    for (fid, y, _), p in zip(m["oof"], pred):
        if fid in now:
            out.append({"face_id": fid, "rating": now[fid], "predicted": round(float(p), 1),
                        "diff": round(float(p - now[fid]), 1)})
    out.sort(key=lambda d: -abs(d["diff"]))
    pids = {r[0]: r[1] for r in db.q(f"SELECT id, photo_id FROM faces WHERE id IN ({','.join(map(str, fids)) or '0'})")}
    for d in out:
        d["photo_id"] = pids.get(d["face_id"])
    return out[:limit]


# ---------------------------------------------------------------- active learning

def _kmeans(X, k, seed=0, iters=15):
    rng = np.random.default_rng(seed)
    C = X[rng.choice(len(X), k, replace=False)]
    for _ in range(iters):
        lab = ((X[:, None, :] - C[None]) ** 2).sum(-1).argmin(1)
        for j in range(k):
            if (lab == j).any():
                C[j] = X[lab == j].mean(0)
    return lab


def select_batch(n=10, exclude=(), seed=None):
    """Face ids to rate next: uncertain + diverse (embedding clusters) + random. No two from one burst."""
    lib = Library()
    if not len(lib):
        return []
    rated = set(current_ratings())
    skipped = {r[0] for r in db.q("SELECT face_id FROM skips WHERE active=1")}
    burst_of = {int(f): b for f, b in zip(lib.face, lib.burst)}
    used_bursts = {burst_of.get(f) for f in rated if burst_of.get(f) is not None}
    cand = [i for i, f in enumerate(lib.face)
            if int(f) not in rated and int(f) not in skipped and int(f) not in exclude
            and (lib.burst[i] is None or lib.burst[i] not in used_bursts)]
    if not cand:
        return []
    rng = np.random.default_rng(seed if seed is not None else int(time.time()))
    _, m = current_model()
    tf, X = (m["tf"], transform(m["tf"], lib)) if m else fit_transform(lib)
    cand = np.array(cand)
    if m:
        boot = X[cand] @ m["boot_w"].T + m["boot_b"]
        unc = boot.std(1)
    else:
        unc = rng.random(len(cand))
    emb = X[:, len(HAND):] if X.shape[1] > len(HAND) else X
    k = int(min(12, max(2, len(lib) // 8)))
    lab = _kmeans(emb, min(k, len(lib)), seed=0) if len(lib) >= 4 else np.zeros(len(lib), int)
    rated_idx = [lib.pos[f] for f in rated if f in lib.pos]
    cl_count = np.bincount(lab[rated_idx], minlength=lab.max() + 1) if rated_idx else np.zeros(lab.max() + 1)
    picked, bursts = [], set()

    def take(i):
        b = lib.burst[i]
        if i in picked or (b is not None and b in bursts):
            return False
        picked.append(i)
        if b is not None:
            bursts.add(b)
        return True

    n_unc, n_div = int(round(n * 0.4)), int(round(n * 0.4))
    for j in np.argsort(-unc):
        if len(picked) >= n_unc:
            break
        take(int(cand[j]))
    for cl in np.argsort(cl_count + rng.random(len(cl_count)) * 0.1):
        if len(picked) >= n_unc + n_div:
            break
        members = [int(i) for i in cand if lab[i] == cl]
        rng.shuffle(members)
        for i in members:
            if take(i):
                cl_count[cl] += 1
                break
    rest = list(cand)
    rng.shuffle(rest)
    for i in rest:
        if len(picked) >= n:
            break
        take(int(i))
    return [int(lib.face[i]) for i in picked]


def select_pair(seed=None):
    """Two photos of me with close predicted scores, from different bursts, not compared before."""
    rows = db.q("SELECT s.face_id, s.score, p.burst FROM scores s JOIN photos p ON p.id=s.photo_id")
    if len(rows) < 2:
        return None
    rng = np.random.default_rng(seed if seed is not None else int(time.time() * 1000) % 2**32)
    done = {(a, b) for a, b, _ in current_pairs()} | {(b, a) for a, b, _ in current_pairs()}
    for _ in range(20):
        a = rows[int(rng.integers(len(rows)))]
        others = [rows[int(i)] for i in rng.choice(len(rows), min(200, len(rows)), replace=False)]
        others = [o for o in others if o[0] != a[0] and (o[2] is None or o[2] != a[2]) and (a[0], o[0]) not in done]
        if not others:
            continue
        b = min(others, key=lambda o: abs(o[1] - a[1]))
        return int(a[0]), int(b[0])
    return None

"""SQLite storage. One connection per thread, WAL mode, so a crash loses at most one open batch."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT, root TEXT, device_id INTEGER,
  enabled INTEGER DEFAULT 1, added REAL);
CREATE TABLE IF NOT EXISTS devices(
  id INTEGER PRIMARY KEY, name TEXT, token_hash TEXT UNIQUE, created REAL, last_seen REAL, source_id INTEGER);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, rel TEXT NOT NULL,
  name TEXT, size INTEGER, mtime REAL, sidecar TEXT, taken_hint REAL, mime TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  want INTEGER DEFAULT 0,
  error TEXT, attempts INTEGER DEFAULT 0, sha256 TEXT, width INTEGER, height INTEGER, photo_id INTEGER,
  UNIQUE(source_id, rel));
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE INDEX IF NOT EXISTS files_photo ON files(photo_id);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS files_want ON files(source_id, want);
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, best_file INTEGER, width INTEGER, height INTEGER, phash TEXT, sha256 TEXT,
  taken_local TEXT, taken_ts REAL, lat REAL, lon REAL, cc TEXT, country TEXT, admin TEXT, city TEXT,
  make TEXT, model TEXT, lens TEXT, focal REAL, focal35 REAL, facing TEXT DEFAULT 'unknown', orientation INTEGER,
  n_faces INTEGER DEFAULT 0, n_people INTEGER DEFAULT 0, burst INTEGER, thumb TEXT, missing INTEGER DEFAULT 0,
  me_face INTEGER, stage2 INTEGER DEFAULT 0, added REAL);
CREATE INDEX IF NOT EXISTS photos_me ON photos(me_face);
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL, x1 REAL, y1 REAL, x2 REAL, y2 REAL, det REAL,
  kps TEXT, size_px REAL, emb BLOB, emb_norm REAL,
  person_id INTEGER, assign TEXT, sim REAL, cluster INTEGER);
CREATE INDEX IF NOT EXISTS faces_photo ON faces(photo_id);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS faces_cluster ON faces(cluster);
CREATE TABLE IF NOT EXISTS face_rejects(face_id INTEGER, person_id INTEGER, PRIMARY KEY(face_id, person_id));
CREATE TABLE IF NOT EXISTS persons(id INTEGER PRIMARY KEY, name TEXT, is_me INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS features(
  face_id INTEGER PRIMARY KEY, photo_id INTEGER, data TEXT, clip_face BLOB, clip_full BLOB, iqa REAL,
  version INTEGER, updated REAL);
CREATE TABLE IF NOT EXISTS ratings(
  id INTEGER PRIMARY KEY, face_id INTEGER, photo_id INTEGER, rating INTEGER, source TEXT, created REAL,
  active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS comparisons(
  id INTEGER PRIMARY KEY, face_a INTEGER, face_b INTEGER, result TEXT, created REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS skips(id INTEGER PRIMARY KEY, face_id INTEGER, created REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY, kind TEXT, row_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS models(
  id INTEGER PRIMARY KEY, created REAL, n_labels INTEGER, n_pairs INTEGER, spearman REAL, blob BLOB,
  current INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS scores(
  face_id INTEGER PRIMARY KEY, photo_id INTEGER, raw REAL, score REAL, conf REAL, prior INTEGER,
  model_id INTEGER, contrib TEXT, score_p REAL);
CREATE INDEX IF NOT EXISTS ratings_face ON ratings(face_id);
CREATE INDEX IF NOT EXISTS scores_photo ON scores(photo_id);
CREATE TABLE IF NOT EXISTS previews(photo_id INTEGER PRIMARY KEY, path TEXT, bytes INTEGER, used REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS photo_vec(photo_id INTEGER PRIMARY KEY, emb BLOB);
"""

MIGRATIONS = ["ALTER TABLE scores ADD COLUMN score_p REAL"]

_local = threading.local()
_db_path: Path | None = None
_init_lock = threading.Lock()


def set_path(path: Path):
    """Use another database file (tests)."""
    global _db_path
    _db_path = Path(path)
    _local.__dict__.clear()


def path() -> Path:
    return _db_path or (config.DATA / "ratedgallery.db")


def connect(p: Path | None = None) -> sqlite3.Connection:
    p = Path(p or path())
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=60, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("PRAGMA foreign_keys=OFF")
    return c


def conn() -> sqlite3.Connection:
    """Thread-local connection (autocommit mode; use tx() for batches)."""
    key = str(path())
    c = getattr(_local, "c", None)
    if c is None or getattr(_local, "key", None) != key:
        c = connect()
        with _init_lock:
            c.executescript(SCHEMA)
            for sql in MIGRATIONS:
                try:
                    c.execute(sql)
                except sqlite3.OperationalError:
                    pass  # already done
        _local.c, _local.key = c, key
    return c


class tx:
    """Explicit transaction: `with db.tx() as c: ...`"""

    def __enter__(self):
        self.c = conn()
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, et, ev, tb):
        if et is None:
            self.c.execute("COMMIT")
        else:
            self.c.execute("ROLLBACK")
        return False


def q(sql, args=()):
    return conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    return conn().execute(sql, args).fetchone()


def val(sql, args=(), default=None):
    r = conn().execute(sql, args).fetchone()
    return default if r is None or r[0] is None else r[0]


def ex(sql, args=()):
    return conn().execute(sql, args)


def kv_get(k, default=None):
    r = q1("SELECT v FROM kv WHERE k=?", (k,))
    if r is None:
        return default
    try:
        return json.loads(r[0])
    except ValueError:
        return default


def kv_set(k, v):
    ex("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def f32(a) -> bytes:
    return np.asarray(a, dtype=np.float32).tobytes()


def unf32(b) -> np.ndarray | None:
    return None if b is None else np.frombuffer(b, dtype=np.float32)


def now() -> float:
    return time.time()

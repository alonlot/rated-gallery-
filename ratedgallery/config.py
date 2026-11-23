"""Paths and user settings. Settings live in data/settings.json."""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("RG_DATA", ROOT / "data")).resolve()
MODELS = Path(os.environ.get("RG_MODELS", DATA / "models")).resolve()
STATIC = Path(__file__).resolve().parent / "static"


def cache_dir(name: str) -> Path:
    p = DATA / "cache" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


DEFAULTS = {
    "workers": max(1, min(5, (os.cpu_count() or 6) - 1)),
    "low_priority": False,
    "map_enabled": False,          # tiles come from the internet; off = fully offline
    "lan_enabled": False,          # default: localhost only
    "lan_password_hash": "",
    "port": 8765,
    "preview_cache_mb": 1024,
    "match_accept": 0.42,          # ArcFace cosine similarity: auto accept
    "match_reject": 0.28,          # below: not this person
    "near_dup_bits": 4,            # pHash distance for "same photo" (removed as duplicate)
    "burst_bits": 14,              # pHash distance for "burst" (kept, grouped)
    "burst_seconds": 15,
    "retrain_every": 10,
    "session_secret": "",
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (DATA / "settings.json")
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        if self.path.exists():
            try:
                self._data.update(json.loads(self.path.read_text("utf-8")))
            except (OSError, ValueError):
                pass
        if not self._data.get("session_secret"):
            self._data["session_secret"] = secrets.token_hex(32)
            self.save()

    def __getitem__(self, k):
        return self._data.get(k, DEFAULTS.get(k))

    def get(self, k, default=None):
        return self._data.get(k, default)

    def update(self, **kw):
        with self._lock:
            self._data.update(kw)
            self.save_locked()

    def save(self):
        with self._lock:
            self.save_locked()

    def save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
        os.replace(tmp, self.path)

    def public(self) -> dict:
        d = {k: v for k, v in self._data.items() if k not in ("lan_password_hash", "session_secret")}
        d["lan_password_set"] = bool(self._data.get("lan_password_hash"))
        return d


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests(path: Path):
    global _settings
    _settings = Settings(path)
    return _settings

"""LAN access: password login (PBKDF2), signed session cookie, device tokens. Localhost needs no login."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import defaultdict, deque

from . import config, db

COOKIE = "rg_session"
SESSION_DAYS = 30
_fails: dict[str, deque] = defaultdict(deque)


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 300_000).hex()
    return f"pbkdf2$300000${salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(got, h)
    except (ValueError, AttributeError):
        return False


def is_local(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "testclient")


def make_session(kind="pw") -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    msg = f"{kind}:{exp}"
    sig = hmac.new(config.settings()["session_secret"].encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}:{sig}"


def check_session(value: str | None) -> bool:
    if not value:
        return False
    try:
        kind, exp, sig = value.rsplit(":", 2)
    except ValueError:
        return False
    msg = f"{kind}:{exp}"
    good = hmac.new(config.settings()["session_secret"].encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig) and int(exp) > time.time()


def too_many_failures(ip: str) -> bool:
    q = _fails[ip]
    while q and q[0] < time.time() - 600:
        q.popleft()
    return len(q) >= 10


def note_failure(ip: str):
    _fails[ip].append(time.time())


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def device_for_token(token: str | None):
    if not token:
        return None
    r = db.q1("SELECT * FROM devices WHERE token_hash=?", (token_hash(token),))
    if r:
        db.ex("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), r["id"]))
    return r

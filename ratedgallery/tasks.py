"""Background task runner. One task at a time, in a background thread, with progress and ETA."""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque

log = logging.getLogger("ratedgallery.tasks")


class Cancelled(Exception):
    pass


class Progress:
    def __init__(self, name: str):
        self.name = name
        self.phase = ""
        self.done = 0
        self.total = 0
        self.started = time.time()
        self.phase_started = time.time()
        self.message = ""
        self.cancel = False
        self._recent = deque(maxlen=200)

    def set_phase(self, phase: str, total: int = 0):
        self.phase, self.total, self.done = phase, total, 0
        self.phase_started = time.time()
        self._recent.clear()

    def step(self, n: int = 1):
        self.done += n
        self._recent.append((time.time(), self.done))
        if self.cancel:
            raise Cancelled()

    def check(self):
        if self.cancel:
            raise Cancelled()

    def rate_per_min(self):
        if len(self._recent) >= 2:
            (t0, d0), (t1, d1) = self._recent[0], self._recent[-1]
            if t1 > t0:
                return (d1 - d0) / (t1 - t0) * 60
        el = time.time() - self.phase_started
        return self.done / el * 60 if el > 0 and self.done else 0.0

    def as_dict(self):
        r = self.rate_per_min()
        eta = (self.total - self.done) / r * 60 if r > 0 and self.total > self.done else None
        return {"name": self.name, "phase": self.phase, "done": self.done, "total": self.total,
                "rate_per_min": round(r, 1), "eta_s": round(eta) if eta is not None else None,
                "elapsed_s": round(time.time() - self.started), "message": self.message}


class Runner:
    """Queue of named tasks. Requests for a task that is already queued are merged."""

    def __init__(self):
        self.queue: deque = deque()
        self.current: Progress | None = None
        self.history: deque = deque(maxlen=20)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        self.funcs = {}

    def register(self, name, fn):
        self.funcs[name] = fn

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="task-runner", daemon=True)
            self.thread.start()

    def request(self, name, front=False, **kw):
        """Queue a task. front=True puts it first (for example training after new ratings)."""
        with self.lock:
            for i, q in enumerate(self.queue):
                if q[0] == name and q[1] == kw:
                    if front and i:
                        del self.queue[i]
                        self.queue.appendleft((name, kw))
                    return False
            if front:
                self.queue.appendleft((name, kw))
            else:
                self.queue.append((name, kw))
        self.wake.set()
        return True

    def others_waiting(self, name) -> bool:
        """True if a task other than `name` is queued. Long tasks check this to make room for it."""
        with self.lock:
            return any(q[0] != name for q in self.queue)

    def pending(self, name) -> bool:
        with self.lock:
            return (self.current is not None and self.current.name == name) or any(q[0] == name for q in self.queue)

    def cancel(self):
        with self.lock:
            self.queue.clear()
            if self.current:
                self.current.cancel = True

    def busy(self):
        return self.current is not None or bool(self.queue)

    def status(self):
        with self.lock:
            return {"current": self.current.as_dict() if self.current else None,
                    "queued": [q[0] for q in self.queue], "history": list(self.history)}

    def run_now(self, name, **kw):
        """Run synchronously in the calling thread (tests, CLI)."""
        p = Progress(name)
        self.current = p
        try:
            return self.funcs[name](p, **kw)
        finally:
            self.current = None

    def _loop(self):
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.queue:
                        break
                    name, kw = self.queue.popleft()
                    p = Progress(name)
                    self.current = p
                t0 = time.time()
                status, err = "ok", None
                try:
                    self.funcs[name](p, **kw)
                except Cancelled:
                    status = "cancelled"
                except Exception as e:  # noqa: BLE001
                    status, err = "error", f"{type(e).__name__}: {e}"
                    log.error("Task %s failed:\n%s", name, traceback.format_exc())
                with self.lock:
                    self.history.appendleft({"name": name, "status": status, "error": err,
                                             "seconds": round(time.time() - t0, 1), "finished": time.time(),
                                             "message": p.message})
                    self.current = None


runner = Runner()

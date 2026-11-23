"""Start the app: python -m ratedgallery  (start.bat does this)."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import threading
import time
import webbrowser

from . import config, offline


def setup_logging():
    logdir = config.DATA / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(logdir / "ratedgallery.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def main():
    ap = argparse.ArgumentParser(description="Rated Gallery")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    offline.enable()
    setup_logging()
    import uvicorn

    from . import server

    log = logging.getLogger("ratedgallery")
    first = True
    while True:
        s = config.settings()
        port = args.port or int(s["port"])
        host = "0.0.0.0" if s["lan_enabled"] else "127.0.0.1"
        cfg = uvicorn.Config(server.app, host=host, port=port, log_level="warning", access_log=False)
        srv = uvicorn.Server(cfg)
        server.RESTART["requested"] = False
        server.RESTART["callback"] = lambda: setattr(srv, "should_exit", True)
        log.info("Rated Gallery on http://127.0.0.1:%d  (LAN access: %s)", port, "ON" if s["lan_enabled"] else "off")
        if first and not args.no_browser:
            threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
        first = False
        srv.run()
        if not server.RESTART["requested"]:
            break
        log.info("Restarting with new network settings")
        time.sleep(0.5)


if __name__ == "__main__":
    main()

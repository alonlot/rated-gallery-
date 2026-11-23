"""Offline guard. Call enable() at startup in every process.

- Sets HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE.
- Blocks outbound socket connections from Python to public addresses. Loopback, private LAN,
  and link-local addresses stay allowed (the phone and the browser are on the LAN).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket

log = logging.getLogger("ratedgallery.offline")
_enabled = False


def _allowed(host) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost",)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified


def _check(address):
    if isinstance(address, tuple) and address:
        host = address[0]
        if not _allowed(host):
            # resolve names; allow only if every result is local
            try:
                infos = socket.getaddrinfo(host, None)
                if infos and all(_allowed(i[4][0]) for i in infos):
                    return
            except OSError:
                pass
            log.warning("Blocked outbound connection to %s", host)
            raise ConnectionRefusedError(f"Rated Gallery offline guard: connection to {host} blocked")


def enable():
    global _enabled
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    if _enabled:
        return
    _enabled = True
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check(address)
        return orig_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check(address)
        return orig_connect_ex(self, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex

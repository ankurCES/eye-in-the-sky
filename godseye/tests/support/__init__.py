"""Test-only helpers for the BYOK provider tests (stub endpoint, deny proxy).

Adapted from the BYOK research scripts (plumbing/stub_anthropic.py and
settings-design/deny_egress_proxy.py). Stdlib only, in-process, loopback only.
"""
from __future__ import annotations

import os
import socket


def pick_port(host: str = "127.0.0.1") -> int:
    """A free port: inside ``GODSEYE_TEST_PORTS=<lo>-<hi>`` when set (agents on
    a shared machine get a port range each), else 0 (the OS picks)."""
    spec = os.environ.get("GODSEYE_TEST_PORTS", "").strip()
    if not spec:
        return 0
    lo, _, hi = spec.partition("-")
    for port in range(int(lo), int(hi or lo) + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in GODSEYE_TEST_PORTS={spec}")

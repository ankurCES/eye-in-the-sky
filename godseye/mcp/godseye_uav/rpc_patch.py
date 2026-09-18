"""Isolated-IOLoop airsim client (fixes the tornado-4.5 `IOLoop is already
running` crash when the msgpack-rpc client is called from a thread whose
asyncio/uvicorn loop is active — e.g. the MCP server's async tool handlers).

tornado 4.5's ``IOLoop.start()`` raises ``RuntimeError("IOLoop is already
running")`` when the current thread already has a running IOLoop/asyncio loop.
airsim's ``MultirotorClient`` shares one ``msgpackrpc.Loop`` (a tornado IOLoop)
across calls, so any RPC issued from the MCP server thread blows up the moment
uvicorn has started its loop there.

The fix: build the client with a **fresh** ``msgpackrpc.Loop`` (which creates a
new tornado ``IOLoop`` and makes it current for the thread at construction
time), and serialize calls through a lock so the per-thread loop is only ever
driven by one RPC at a time. Construct the client inside the worker thread that
will issue calls, not the main thread.
"""

from __future__ import annotations

import threading


def make_client(ip: str = "127.0.0.1", port: int = 41451, timeout_value: int = 3600):
    """Return an airsim.MultirotorClient on its own tornado IOLoop + a lock.

    Usage: ``client, lock = make_client(...)`` then wrap blocking calls in
    ``with lock:`` when sharing the client across threads. Each thread that
    will issue RPCs should construct its own client via this factory.
    """
    import msgpackrpc
    from msgpackrpc.loop import Loop
    import airsim

    loop = Loop()  # new tornado IOLoop, made current for THIS thread
    address = msgpackrpc.Address(ip, port)
    # unpack raw=False so msgpack decodes map keys to str (msgpack>=0.6 default
    # is raw=True -> bytes keys, which breaks airsim's from_msgpack setattr).
    raw = msgpackrpc.Client(address, timeout=timeout_value, loop=loop,
                            unpack_encoding="utf-8")
    # Rebind the airsim convenience wrapper onto the isolated raw client.
    client = airsim.MultirotorClient.__new__(airsim.MultirotorClient)
    client.client = raw
    return client, threading.Lock()

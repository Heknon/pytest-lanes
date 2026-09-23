"""Compatibility with third-party plugins that assume one test at a time per process.

C1 pytest-rerunfailures >= 15: under xdist each worker process gets one socket to
the controller's rerun database (``config.failures_db``, a ``ClientStatusDB``),
and its methods send a request and read the reply on that socket. A hybrid
worker's lanes share it, so their exchanges interleaved and a lane died with
``ValueError`` (INTERNALERROR). Each of its socket methods is serialized here.
"""
from __future__ import annotations

import contextlib
import functools
import threading

#: The ClientStatusDB methods that talk on the socket.
RERUN_DB_METHODS = ("_get", "_set", "increment_suite_reruns")


def rerunfailures_client(config):
    db = getattr(config, "failures_db", None)
    return db if type(db).__name__ == "ClientStatusDB" else None


@contextlib.contextmanager
def serialized_rerunfailures_client(config):
    db = rerunfailures_client(config)
    if db is None:
        yield
        return
    lock = threading.RLock()
    wrapped = []
    for name in RERUN_DB_METHODS:
        method = getattr(db, name, None)
        if method is None:
            continue

        def locked(*args, _method=method, **kwargs):
            with lock:
                return _method(*args, **kwargs)

        setattr(db, name, functools.wraps(method)(locked))
        wrapped.append(name)
    try:
        yield
    finally:
        for name in wrapped:
            db.__dict__.pop(name, None)

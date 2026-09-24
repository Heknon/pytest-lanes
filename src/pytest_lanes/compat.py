"""Compatibility with third-party plugins that assume one test at a time per process.

C2 pytest-rerunfailures >= 16: its module-level ``suspended_finalizers`` is made per
lane (``per_lane_rerun_suspended_finalizers``).

C1 pytest-rerunfailures >= 15: under xdist each worker process gets one socket to
the controller's rerun database (``config.failures_db``, a ``ClientStatusDB``),
and its methods send a request and read the reply on that socket. A hybrid
worker's lanes share it, so their exchanges interleaved and a lane died with
``ValueError`` (INTERNALERROR). All of its methods are serialized here.
"""
from __future__ import annotations

import contextlib
import functools
import threading

from .lane import LANE

def rerunfailures_client(config):
    db = getattr(config, "failures_db", None)
    return db if type(db).__name__ == "ClientStatusDB" else None


def rerun_db_methods(db) -> list:
    """Every method of the client's classes: each may talk on the socket (16.7 added
    try_increment/decrement_suite_reruns, which a fixed list missed). Re-entrant lock:
    they call one another."""
    names = set()
    for cls in type(db).__mro__:
        if cls is object:
            continue
        for name, value in vars(cls).items():
            if callable(value) and not (name.startswith("__") and name.endswith("__")):
                names.add(name)
    return sorted(names)


@contextlib.contextmanager
def serialized_rerunfailures_client(config):
    db = rerunfailures_client(config)
    if db is None:
        yield
        return
    lock = threading.RLock()
    wrapped = []
    for name in rerun_db_methods(db):
        method = getattr(db, name)

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


# ------------------------------------------------------------------ C2
class _PerLaneDict(dict):
    """A module-level dict whose contents are per lane (the main thread has its own).

    Every access goes through the overridden methods; ``dict.update(this)`` takes the
    generic path (keys() and __getitem__) because ``__iter__`` is overridden."""

    def __init__(self, initial=None):
        super().__init__()
        self._stores = {}
        self._lock = threading.Lock()
        if initial:
            self._store().update(initial)

    def _store(self) -> dict:
        key = LANE.get()
        store = self._stores.get(key)
        if store is None:
            with self._lock:
                store = self._stores.setdefault(key, {})
        return store

    def __getitem__(self, k): return self._store()[k]
    def __setitem__(self, k, v): self._store()[k] = v
    def __delitem__(self, k): del self._store()[k]
    def __contains__(self, k): return k in self._store()
    def __iter__(self): return iter(list(self._store()))
    def __len__(self): return len(self._store())
    def __bool__(self): return bool(self._store())
    def __repr__(self): return repr(self._store())
    def keys(self): return list(self._store().keys())
    def values(self): return list(self._store().values())
    def items(self): return list(self._store().items())
    def get(self, k, default=None): return self._store().get(k, default)
    def pop(self, k, *default): return self._store().pop(k, *default)
    def setdefault(self, k, default=None): return self._store().setdefault(k, default)
    def update(self, *args, **kwargs): self._store().update(*args, **kwargs)
    def clear(self): self._store().clear()
    def copy(self): return dict(self._store())


def rerunfailures_module():
    try:
        import pytest_rerunfailures
    except ImportError:
        return None
    return pytest_rerunfailures


@contextlib.contextmanager
def per_lane_rerun_suspended_finalizers():
    """C2 pytest-rerunfailures >= 16: ``suspended_finalizers``, a module-level dict.

    Before rerunning a test it moves the setup stack above the test into this dict, and
    every test's teardown moves the dict back into its own SetupState. Shared by lanes,
    another lane's test took those entries: a module's fixtures were torn down under
    running tests, or never (the run stayed green). Each lane now has its own.
    """
    rf = rerunfailures_module()
    original = getattr(rf, "suspended_finalizers", None) if rf is not None else None
    if not isinstance(original, dict):
        yield
        return
    rf.suspended_finalizers = _PerLaneDict(original)
    try:
        yield
    finally:
        rf.suspended_finalizers = original

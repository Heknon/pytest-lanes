"""Compatibility with third-party plugins that assume one test at a time per process.

C4 pytest-cov: in one process it measures with its local engine (``Central``), which
already traces every lane's thread; its xdist node hooks drive its distributed engine
(``DistMaster``), which is not running, and fail on the local one. Lanes' node hooks
skip it in exactly that case (``node_hook_callers``), as plain pytest-cov never sees
node hooks without ``-n``.

C3 pytest-metadata: under xdist its worker-side ``pytest_configure`` puts the metadata in
``config.workeroutput``, and the controller's ``pytest_testnodedown`` reads it from each
node. A lane has no worker-side configure (one process configures once), so each lane's
``workeroutput`` is seeded with it (``seed_worker_output``).

C2 pytest-rerunfailures >= 15: its module-level ``suspended_finalizers`` is made per
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
    """C2 pytest-rerunfailures >= 15: ``suspended_finalizers``, a module-level dict.

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


# ---------------------------------------------------------------- C3 pytest-metadata
def metadata_plugin_key(config):
    """pytest-metadata's stash key, when the plugin is active (None otherwise)."""
    plugin = config.pluginmanager.get_plugin("metadata")
    return getattr(plugin, "metadata_key", None) if plugin is not None else None


def seed_worker_output(config, workeroutput: dict) -> None:
    """C3: what worker-side plugins write into ``config.workeroutput`` during their
    ``pytest_configure``, which a lane never runs (the process configured once)."""
    key = metadata_plugin_key(config)
    if key is not None and key in config.stash:
        workeroutput["metadata"] = config.stash[key]


# ---------------------------------------------------------------- C4 pytest-cov
NODE_HOOKS = ("pytest_configure_node", "pytest_testnodeready",
              "pytest_xdist_node_collection_finished", "pytest_testnodedown")


def local_coverage_plugin(config):
    """pytest-cov's plugin when it measures locally (no distributed engine), else None."""
    plugin = config.pluginmanager.get_plugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    if controller is None or hasattr(controller, "configure_node"):
        return None
    return plugin


def node_hook_callers(config) -> dict:
    """xdist's controller hooks for lanes (single-process mode): every plugin, except
    pytest-cov's hooks while it measures locally (C4)."""
    skip = [p for p in (local_coverage_plugin(config),) if p is not None]
    pm = config.pluginmanager
    return {name: pm.subset_hook_caller(name, remove_plugins=skip) if skip else getattr(config.hook, name)
            for name in NODE_HOOKS}

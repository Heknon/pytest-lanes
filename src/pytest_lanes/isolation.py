"""Give each lane the private pytest state an xdist worker process would have.

pytest keeps per-run state in places that assume one test runs at a time. Each
function here re-keys one of them by the current lane (the ``LANE`` contextvar),
or makes it safe to share. Each one is a numbered touchpoint in CLAUDE.md and
DESIGN.md, and ``probes.py`` checks it at startup:

* P1 ``session._setupstate``: one SetupState per lane.
* P2 ``FixtureDef.cached_result`` / ``_finalizers`` / ``cached_param``: fixture caches per lane.
* P6 ``_pytest.runner._update_current_test_var``: ``PYTEST_CURRENT_TEST`` race.
* P7 ``config._tmp_path_factory``: a basetemp per lane, as xdist gives each worker.
* P10 ``config.workerinput`` / ``workeroutput``: each lane is its own xdist worker.
* P11 ``_pytest.recwarn.WarningsRecorder.__enter__``: fail closed on pytest.warns
  before Python 3.14 (warning state is process-wide there).
* P12 ``Cache.get``/``Cache.set``: serialized, so a concurrent read never sees a half-written value.
* P3 and P8 (logging) live in ``capture.py``; C1 (third-party plugins) in ``compat.py``.

``isolate_lanes()`` installs all of them together with the capture of
``capture.py`` and undoes them on exit.
"""
from __future__ import annotations

import contextlib
import copy
import functools
import os
import sys
import threading
from dataclasses import dataclass

from .capture import (
    clone_log_handlers,
    per_lane_logging,
    per_lane_std_streams,
    snapshot_logger_dict,
)
from .compat import serialized_rerunfailures_client
from .lane import LANE


# ------------------------------------------------------------ P1: setup state
class _SetupStateRouter:
    """Replaces ``session._setupstate``; forwards to the current lane's own SetupState."""

    def __init__(self, main) -> None:
        object.__setattr__(self, "_main", main)

    def _target(self):
        lane = LANE.get()
        return self._main if lane is None else lane.setupstate

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        setattr(self._target(), name, value)


@contextlib.contextmanager
def per_lane_setupstate(session):
    """Yields the SetupState class, so that each new lane can build its own."""
    main = session._setupstate
    session._setupstate = _SetupStateRouter(main)
    try:
        yield type(main)
    finally:
        session._setupstate = main


# ------------------------------------------------------------ P2: fixture caches
def patch_fixturedef() -> None:
    """Turn FixtureDef's cache attributes into properties keyed by the current lane.

    Permanent and idempotent: FixtureDef instances outlive the session, and the
    main thread (no lane) keeps using per-instance storage exactly as before.
    """
    from _pytest.fixtures import FixtureDef

    if FixtureDef.__dict__.get("_lanes_patched"):
        return

    def slot(self):  # -> [cached_result, finalizers] for this lane
        lane = LANE.get()
        if lane is None:
            d = self.__dict__
            s = d.get("_lanes_main")
            if s is None:  # migrate pre-existing instance state
                s = d["_lanes_main"] = [d.pop("cached_result", None), d.pop("_finalizers", [])]
            return s
        s = lane.fixture_state.get(self)
        if s is None:
            s = lane.fixture_state[self] = [None, []]
        return s

    def _set_cr(self, v):
        slot(self)[0] = v

    def _set_fin(self, v):
        slot(self)[1] = v

    FixtureDef.cached_result = property(lambda self: slot(self)[0], _set_cr)
    FixtureDef._finalizers = property(lambda self: slot(self)[1], _set_fin)
    for name in LANE_FIXTUREDEF_ATTRS:
        setattr(FixtureDef, name, _PerLaneAttribute(name))
    FixtureDef._lanes_patched = True


#: FixtureDef attributes that plugins set on setup and delete on finalization: pytest's
#: setuponly plugin (--setup-show/--setup-only) keeps the fixture's param there.
LANE_FIXTUREDEF_ATTRS = ("cached_param",)


class _PerLaneAttribute:
    """A FixtureDef attribute that each lane sets, reads and deletes on its own (P2).

    Missing reads as missing (AttributeError), so ``hasattr``/``del`` keep working.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    @staticmethod
    def _store(obj) -> dict:
        lane = LANE.get()
        if lane is None:
            return obj.__dict__.setdefault("_lanes_main_attrs", {})
        return lane.fixture_state.setdefault((obj, "attrs"), {})

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        try:
            return self._store(obj)[self.name]
        except KeyError:
            raise AttributeError(self.name) from None

    def __set__(self, obj, value) -> None:
        self._store(obj)[self.name] = value

    def __delete__(self, obj) -> None:
        try:
            del self._store(obj)[self.name]
        except KeyError:
            raise AttributeError(self.name) from None


# ------------------------------------------------------------ P6: PYTEST_CURRENT_TEST
@contextlib.contextmanager
def race_free_current_test_var():
    """pytest pops PYTEST_CURRENT_TEST without a default, so two lanes finishing
    together raised KeyError in teardown (seen 3 in 1,000 under load)."""
    from _pytest import runner

    original = runner._update_current_test_var

    def _update_current_test_var(item, when):
        if when:
            original(item, when)
        else:
            # Not os.environ.pop(k, None): MutableMapping.pop is check-then-delete, and
            # another lane can delete in between (routinely on free-threaded 3.14t).
            with contextlib.suppress(KeyError):
                del os.environ["PYTEST_CURRENT_TEST"]

    runner._update_current_test_var = _update_current_test_var
    try:
        yield
    finally:
        runner._update_current_test_var = original


# ------------------------------------------------------------ P7: basetemp
class _TempPathFactoryRouter:
    """Replaces ``config._tmp_path_factory``; forwards to the current lane's own factory.

    xdist gives every worker its own basetemp (``<root>/popen-gwN``), so a
    per-worker session fixture may, for example, ``mktemp("db", numbered=False)``,
    while ``getbasetemp().parent`` is the run's root, shared by all workers (the
    xdist-documented place for cross-worker files and locks). Each lane gets the
    same shape, created lazily on first use like pytest's own:

    * single process, whose basetemp is the root: ``<root>/ln3``;
    * hybrid worker, whose basetemp is ``<root>/popen-gw0``: ``<root>/popen-gw0.ln3``.
    """

    def __init__(self, main, in_worker: bool) -> None:
        object.__setattr__(self, "_main", main)
        object.__setattr__(self, "_in_worker", in_worker)

    def _target(self):
        lane = LANE.get()
        if lane is None:
            return self._main
        if lane.tmp_path_factory is None:
            lane.tmp_path_factory = _lane_factory(self._main, lane.gateway.id, self._in_worker)
        return lane.tmp_path_factory

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        setattr(self._target(), name, value)


def _lane_factory(main, lane_id: str, in_worker: bool):
    process_base = main.getbasetemp()
    if in_worker:   # lane_id "gw0.ln3" -> sibling of popen-gw0: popen-gw0.ln3
        given = process_base.parent / f"{process_base.name}.{lane_id.rsplit('.', 1)[-1]}"
    else:
        given = process_base / lane_id
    factory = copy.copy(main)
    factory.__dict__.pop("getbasetemp", None)          # the process-level lock wrapper
    factory._given_basetemp = given
    factory._basetemp = None
    return factory


@contextlib.contextmanager
def per_lane_basetemp(config):
    """Route tmp_path/tmp_path_factory (and legacy tmpdir_factory) to per-lane factories.

    The process's own basetemp is still created lazily, and pytest creates it
    without a lock: with --basetemp (always set on xdist workers) two lanes' first
    use both rmtree+mkdir it. So its getbasetemp is serialized.
    """
    main = getattr(config, "_tmp_path_factory", None)   # absent under -p no:tmpdir
    if main is None:
        yield
        return
    unlocked, lock = main.getbasetemp, threading.Lock()

    def getbasetemp():
        with lock:
            return unlocked()

    main.getbasetemp = getbasetemp
    router = _TempPathFactoryRouter(main, in_worker=hasattr(config, "workerinput"))
    config._tmp_path_factory = router
    legacy = getattr(config, "_tmpdirhandler", None)    # pytest's legacypath plugin
    if legacy is not None:
        legacy._tmppath_factory = router
    try:
        yield
    finally:
        config._tmp_path_factory = main
        if legacy is not None:
            legacy._tmppath_factory = main
        main.__dict__.pop("getbasetemp", None)


# ------------------------------------------------------------ P10: worker identity
def _lane_config_class(base: type) -> type:
    """A subclass of the run's Config class whose workerinput/workeroutput are per lane.

    xdist's worker_id and testrun_uid fixtures and xdist.get_xdist_worker_id() all
    read ``config.workerinput``; suites name per-worker resources after them. On a
    lane these return the lane's (``ThreadNode.workerinput``). Elsewhere they are
    what they were: absent on the single-process main thread (it keeps the
    controller role), the process's own in a hybrid worker.
    """

    def per_lane(attr):
        def get(self):
            lane = LANE.get()
            if lane is not None:
                return getattr(lane, attr)
            try:
                return self.__dict__[attr]
            except KeyError:
                raise AttributeError(attr) from None

        def set_(self, value):
            self.__dict__[attr] = value

        def delete(self):
            del self.__dict__[attr]

        return property(get, set_, delete)

    return type(base.__name__, (base,), {
        "__qualname__": base.__qualname__, "__module__": base.__module__,
        "workerinput": per_lane("workerinput"), "workeroutput": per_lane("workeroutput"),
    })


@contextlib.contextmanager
def per_lane_worker_identity(config):
    original = type(config)
    config.__class__ = _lane_config_class(original)
    try:
        yield
    finally:
        config.__class__ = original


# ------------------------------------------------------------ P11: pytest.warns
@contextlib.contextmanager
def guard_warnings_recorder(is_exclusive):
    """Fail a non-exclusive test that enters pytest.warns / deprecated_call / recwarn.

    Without context-aware warnings (Python < 3.14, or the flag off) these swap the
    process-wide warning filters and showwarning, so concurrent tests corrupted each
    other: 5 of 6 concurrent pytest.warns blocks failed. They cannot be seen at
    collection time, so the test fails deterministically on use, telling the user
    to mark it lanes_exclusive (or to use Python 3.14 context-aware warnings).
    """
    if getattr(sys.flags, "context_aware_warnings", False):
        yield
        return
    import pytest
    from _pytest.recwarn import WarningsRecorder

    original = WarningsRecorder.__dict__["__enter__"]

    def __enter__(self):
        lane = LANE.get()
        if lane is not None and lane.current_item is not None and not is_exclusive(lane.current_item):
            pytest.fail("pytest-lanes: pytest.warns/deprecated_call/recwarn change process-wide "
                        "warning state before Python 3.14, so this test must run alone: mark it "
                        "@pytest.mark.lanes_exclusive (or run on Python 3.14+ with "
                        "-X context_aware_warnings=1).", pytrace=False)
        return original(self)

    WarningsRecorder.__enter__ = __enter__
    try:
        yield
    finally:
        WarningsRecorder.__enter__ = original


# ------------------------------------------------------------ all together
@dataclass
class LaneStateFactory:
    """Builds the private state for each new lane, shaped like the main thread's."""

    setupstate_cls: type
    log_templates: dict

    def setupstate(self):
        return self.setupstate_cls()

    def log_handlers(self) -> dict:
        return clone_log_handlers(self.log_templates)


# ------------------------------------------------------------------ P12: config.cache
CACHE_METHODS = ("get", "set")


@contextlib.contextmanager
def serialized_cache():
    """P12: ``Cache.get``/``Cache.set`` under one process-wide lock.

    pytest writes a value by truncating its file and then writing it, so a lane
    reading at the same moment saw an empty file and got the default (45 of 200
    concurrent get-after-set tests failed). Between xdist processes the same race
    exists; within a process, lanes make it likely, so it is serialized here.
    """
    from _pytest.cacheprovider import Cache

    lock = threading.RLock()
    originals = {name: Cache.__dict__[name] for name in CACHE_METHODS}

    def locked(fn):
        @functools.wraps(fn)
        def method(self, *args, **kwargs):
            with lock:
                return fn(self, *args, **kwargs)
        return method

    for name, fn in originals.items():
        setattr(Cache, name, locked(fn))
    try:
        yield
    finally:
        for name, fn in originals.items():
            setattr(Cache, name, fn)


@contextlib.contextmanager
def isolate_lanes(config, session, is_exclusive):
    """Install every per-lane isolation; yields a ``LaneStateFactory``."""
    patch_fixturedef()                                                   # P2
    with contextlib.ExitStack() as stack:
        stack.enter_context(race_free_current_test_var())                # P6
        stack.enter_context(per_lane_worker_identity(config))            # P10
        stack.enter_context(guard_warnings_recorder(is_exclusive))       # P11
        stack.enter_context(per_lane_basetemp(config))                   # P7
        setupstate_cls = stack.enter_context(per_lane_setupstate(session))  # P1
        log_templates = stack.enter_context(per_lane_logging(config))    # P3
        stack.enter_context(snapshot_logger_dict())                       # P8
        stack.enter_context(serialized_rerunfailures_client(config))      # C1
        stack.enter_context(serialized_cache())                           # P12
        if config.getoption("capture") != "no":                          # -s: no capture, as xdist
            stack.enter_context(per_lane_std_streams())
        yield LaneStateFactory(setupstate_cls, log_templates)

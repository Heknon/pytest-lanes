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
* P14 ``unittest.mock._patch.__enter__`` / ``_patch_dict._patch_dict`` and ``pytest.MonkeyPatch``:
  fail closed on a process-wide patch in a test that is not exclusive.
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
import types
from dataclasses import dataclass

import pytest

from .capture import (
    clone_log_handlers,
    fixture_follows_global_suspend,
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
        attrs = lane.fixture_attrs.get(obj)
        if attrs is None:
            attrs = lane.fixture_attrs[obj] = {}
        return attrs

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


# ------------------------------------------------------------------ P14: process-wide patches
PATCH_GUARD_MESSAGE = (
    "pytest-lanes: {what} patches process-wide state: every test running on another lane "
    "sees it until it is undone. Mark this test @pytest.mark.lanes_exclusive (it then runs "
    "alone), or, if nothing another test runs uses what it patches, "
    "@pytest.mark.lanes_allow_patches. --lanes-allow-patches turns this check off.")
PATCH_GUARD_SESSION_MESSAGE = (
    "pytest-lanes: {what} in a {scope}-scoped fixture patches process-wide state, and each "
    "lane has its own copy of that fixture: the first lane to finish tears it down and "
    "undoes the patch while other lanes still run. For a value the whole run needs, set it "
    "once in pytest_configure or pytest_sessionstart (conftest.py). --lanes-allow-patches "
    "turns this check off.")


def check_p14():
    """Probe: unittest.mock's patchers still look as ``guard_process_patches`` expects."""
    from unittest import mock

    patch_cls, dict_cls = getattr(mock, "_patch", None), getattr(mock, "_patch_dict", None)
    if patch_cls is None or "__enter__" not in vars(patch_cls):
        return "P14 unittest.mock._patch.__enter__"
    if dict_cls is None or not callable(vars(dict_cls).get("_patch_dict")):
        return "P14 unittest.mock._patch_dict._patch_dict"
    probe = mock.patch("os.sep")
    if not (callable(getattr(probe, "getter", None)) and getattr(probe, "attribute", None) == "sep"):
        return "P14 unittest.mock._patch.getter/.attribute"
    if not _patch_by_path(probe) or _patch_by_path(mock.patch.object(os, "sep")):
        return "P14 unittest.mock._patch.getter no longer tells a dotted path from an object"
    if not hasattr(mock.patch.dict({}), "in_dict"):
        return "P14 unittest.mock._patch_dict.in_dict"
    return None


def _shared(target) -> bool:
    """A module or a class is shared by every lane; an instance, or a class defined
    inside a function (``<locals>``) that no module holds, is presumed the test's own."""
    if isinstance(target, types.ModuleType):
        return True
    if not isinstance(target, type):
        return False
    if "<locals>" not in getattr(target, "__qualname__", ""):
        return True
    module = sys.modules.get(getattr(target, "__module__", None) or "")
    return module is not None and any(v is target for v in list(vars(module).values()))


def _patch_by_path(patcher) -> bool:
    """``mock.patch("pkg.mod.obj.attr")``: whatever the path reaches is a global."""
    getter = patcher.getter
    if isinstance(getter, functools.partial):          # partial(pkgutil.resolve_name, path)
        return bool(getter.args) and isinstance(getter.args[0], str)
    cells = getattr(getter, "__closure__", None) or ()  # older: lambda: _importer(path)
    return any(isinstance(c.cell_contents, str) for c in cells)


def _from_pytest(frame) -> bool:
    """The patch is pytest's twisted support (its unittest plugin patches twisted's
    Failure around each test). Only that module: pytester, also pytest's, changes the
    cwd and environment for the whole process and stays guarded."""
    return frame.f_globals.get("__name__") == "_pytest.unittest"



def _shared_mapping(mapping) -> bool:
    return mapping is os.environ or mapping is sys.modules or isinstance(mapping, str)


@contextlib.contextmanager
def guard_process_patches(config, is_exclusive):
    """P14: a process-wide patch in a test that is not exclusive fails at once.

    Covers ``unittest.mock`` (so pytest-mock) and ``pytest.MonkeyPatch``: a patch of a
    module or class attribute (or by dotted path), of ``os.environ``/``sys.modules``,
    and ``chdir``/``syspath_prepend``. Not guarded: patches of instances (presumed the
    test's own), exclusive tests, ``lanes_allow_patches`` tests, and processes with one
    lane. Patches in session-scoped fixtures are guarded too: each lane tears its own
    fixture down when it finishes, undoing the patch for lanes still running (seen as a
    KeyError). Direct assignments (``os.environ[k] = v``) cannot be seen: ``--lanes-detect``.
    """
    if config.getoption("lanes_allow_patches") or config.getini("lanes_allow_patches") \
            or (config.getoption("lanes") or 0) <= 1:
        yield
        return

    from unittest import mock

    def check(what: str, frame) -> None:
        lane = LANE.get()
        if lane is None or _from_pytest(frame):
            return
        item = lane.current_item
        if item is None or lane.gateway.id == "ln-serial":   # the serial phase runs alone
            return
        # A fixture broader than the test outlives it, so even an exclusive test's
        # module or session fixture leaves its patch in place for the lane's next tests
        # (hybrid mode runs exclusive tests between others).
        scope = lane.fixture_scopes[-1] if lane.fixture_scopes else "function"
        if scope in ("session", "package", "module", "class"):
            pytest.fail(PATCH_GUARD_SESSION_MESSAGE.format(what=what, scope=scope), pytrace=False)
        if is_exclusive(item) or item.get_closest_marker("lanes_allow_patches"):
            return
        pytest.fail(PATCH_GUARD_MESSAGE.format(what=what), pytrace=False)

    notset = object()
    originals = []

    def install(owner, name, make):
        original = vars(owner)[name]
        originals.append((owner, name, original))
        setattr(owner, name, make(original))

    def mock_enter(original):
        def __enter__(self):
            target = self.getter()
            if _patch_by_path(self) or _shared(target):
                check(f"mock.patch of {getattr(target, '__name__', type(target).__name__)}"
                      f".{self.attribute}", sys._getframe(1))
            return original(self)
        return __enter__

    def mock_patch_dict(original):
        def _patch_dict(self):
            if _shared_mapping(self.in_dict):
                name = self.in_dict if isinstance(self.in_dict, str) else \
                    ("os.environ" if self.in_dict is os.environ else "sys.modules")
                check(f"mock.patch.dict of {name}", sys._getframe(1))
            return original(self)
        return _patch_dict

    def mp_attr(verb):
        def make(original):
            def method(self, target, name=notset, *args, **kwargs):
                if isinstance(target, str):        # a dotted path: always a global
                    check(f"monkeypatch.{verb}({target!r})", sys._getframe(1))
                elif _shared(target):
                    check(f"monkeypatch.{verb} of {getattr(target, '__name__', '?')}.{name}",
                          sys._getframe(1))
                return original(self, target, *((name,) if name is not notset else ()), *args, **kwargs)
            return method
        return make

    def mp_item(verb):
        def make(original):
            def method(self, dic, name, *args, **kwargs):
                if dic is os.environ or dic is sys.modules:
                    check(f"monkeypatch.{verb} of {'os.environ' if dic is os.environ else 'sys.modules'}",
                          sys._getframe(1))
                return original(self, dic, name, *args, **kwargs)
            return method
        return make

    def mp_always(label):
        def make(original):
            def method(self, *args, **kwargs):
                check(label, sys._getframe(1))
                return original(self, *args, **kwargs)
            return method
        return make

    mp = pytest.MonkeyPatch
    install(mock._patch, "__enter__", mock_enter)
    install(mock._patch_dict, "_patch_dict", mock_patch_dict)
    install(mp, "setattr", mp_attr("setattr"))
    install(mp, "delattr", mp_attr("delattr"))
    install(mp, "setitem", mp_item("setitem"))
    install(mp, "delitem", mp_item("delitem"))
    install(mp, "setenv", mp_always("monkeypatch.setenv"))
    install(mp, "delenv", mp_always("monkeypatch.delenv"))
    install(mp, "chdir", mp_always("monkeypatch.chdir"))
    install(mp, "syspath_prepend", mp_always("monkeypatch.syspath_prepend"))
    try:
        yield
    finally:
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)


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
        stack.enter_context(guard_process_patches(config, is_exclusive))  # P14
        stack.enter_context(fixture_follows_global_suspend(config))      # P15
        stack.enter_context(per_lane_basetemp(config))                   # P7
        setupstate_cls = stack.enter_context(per_lane_setupstate(session))  # P1
        log_templates = stack.enter_context(per_lane_logging(config))    # P3
        stack.enter_context(snapshot_logger_dict())                       # P8
        stack.enter_context(serialized_rerunfailures_client(config))      # C1
        stack.enter_context(serialized_cache())                           # P12
        if config.getoption("capture") != "no":                          # -s: no capture, as xdist
            stack.enter_context(per_lane_std_streams())
        yield LaneStateFactory(setupstate_cls, log_templates)

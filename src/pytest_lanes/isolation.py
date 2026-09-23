"""Give each lane the private pytest state an xdist worker process would have.

pytest keeps per-run state in places that assume one test runs at a time. Each
function here re-keys one of them by the current lane (the ``LANE`` contextvar),
or makes it safe to share. Each one is a numbered touchpoint in CLAUDE.md and
DESIGN.md, and ``probes.py`` checks it at startup:

* P1 ``session._setupstate``: one SetupState per lane.
* P2 ``FixtureDef.cached_result`` / ``_finalizers``: fixture caches per lane.
* P6 ``_pytest.runner._update_current_test_var``: ``PYTEST_CURRENT_TEST`` race.
* P7 ``config._tmp_path_factory.getbasetemp``: basetemp creation race.
* P3 and P8 (logging) live in ``capture.py``.

``isolate_lanes()`` installs all of them together with the capture of
``capture.py`` and undoes them on exit.
"""
from __future__ import annotations

import contextlib
import os
import threading
from dataclasses import dataclass

from .capture import (
    clone_log_handlers,
    per_lane_logging,
    per_lane_std_streams,
    snapshot_logger_dict,
)
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
    FixtureDef._lanes_patched = True


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
@contextlib.contextmanager
def locked_basetemp(config):
    """pytest creates basetemp lazily and without a lock. With --basetemp (always set
    on xdist workers), two lanes' first tmp_path both rmtree+mkdir it. Serialize it
    on this run's factory instance; creation stays lazy and exactly as pytest does it."""
    tpf = getattr(config, "_tmp_path_factory", None)  # absent under -p no:tmpdir
    if tpf is None:
        yield
        return
    unlocked, lock = tpf.getbasetemp, threading.Lock()

    def getbasetemp():
        with lock:
            return unlocked()

    tpf.getbasetemp = getbasetemp
    try:
        yield
    finally:
        tpf.__dict__.pop("getbasetemp", None)


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


@contextlib.contextmanager
def isolate_lanes(config, session):
    """Install every per-lane isolation; yields a ``LaneStateFactory``."""
    patch_fixturedef()                                                   # P2
    with contextlib.ExitStack() as stack:
        stack.enter_context(race_free_current_test_var())                # P6
        stack.enter_context(locked_basetemp(config))                     # P7
        setupstate_cls = stack.enter_context(per_lane_setupstate(session))  # P1
        log_templates = stack.enter_context(per_lane_logging(config))    # P3
        stack.enter_context(snapshot_logger_dict())                       # P8
        if config.getoption("capture") != "no":                          # -s: no capture, as xdist
            stack.enter_context(per_lane_std_streams())
        yield LaneStateFactory(setupstate_cls, log_templates)

"""Hybrid mode, controller side (``-n P --lanes M``): real xdist, virtual lanes.

The controller is xdist's own ``DSession``, with real worker processes, crash
detection and worker replacement. We wrap whatever scheduler
``pytest_xdist_make_scheduler`` returns in a ``LaneMux``:

* toward ``DSession``, the mux presents the P real workers;
* toward the scheduler, it presents P x M ``LaneProxy`` virtual nodes.

``LaneProxy.send_runtest_some()`` becomes a ``lanes_runtests`` command to the
worker process (handled in ``worker.py``), and ``mark_test_complete`` is routed
back to the lane that owns the item.

Touchpoints: X3 (``DSession.handle_crashitem``) and X4 (the WorkerController
attributes that ``LaneProxy`` mirrors).
"""
from __future__ import annotations

import contextlib
import os
import sys
from types import SimpleNamespace

import pytest

from .scheduling import reject_unsupported


class LaneProxy:
    """Virtual node: one lane inside one xdist worker process.

    Carries the worker-facing attributes of xdist's WorkerController, so that a
    custom scheduler (or a plugin) reading them sees a worker. ``workerinput`` is
    the process's own, with this lane's id and the total lane count.
    ``workerinfo`` and ``workeroutput`` are the process's.
    """

    def __init__(self, wc, lane: int, mux) -> None:
        self.wc = wc              # xdist's WorkerController for the process
        self.lane = lane
        self.mux = mux
        self.gateway = SimpleNamespace(id=f"{wc.gateway.id}.ln{lane}", spec=wc.gateway.spec)
        self.workerinput = {**wc.workerinput, "workerid": self.gateway.id,
                            "workercount": mux.total_lanes}
        self._shutting_down = False

    @property
    def shutting_down(self) -> bool:
        """Also when xdist knows the worker is down or told to shut down (its own
        ``shutting_down``): a completion still queued from a sibling lane must not make
        the scheduler send work to a dead worker (INTERNALERROR), or to one draining
        after ``--maxfail``."""
        return self._shutting_down or bool(getattr(self.wc, "shutting_down", False))

    @shutting_down.setter
    def shutting_down(self, value: bool) -> None:
        self._shutting_down = value

    @property
    def workerinfo(self):  # set on the WorkerController by DSession before add_node
        return {**self.wc.workerinfo, "id": self.gateway.id, "spec": self.gateway.spec}

    @property
    def workeroutput(self):  # like the WorkerController's: exists once the process finished
        return self.wc.workeroutput

    def send_runtest_some(self, indices) -> None:
        indices = list(indices)
        for i in indices:
            self.mux.owner[i] = self
        self.wc.sendcommand("lanes_runtests", lane=self.lane, indices=indices)

    def send_runtest_all(self) -> None:
        raise NotImplementedError("--dist each is not supported by lanes")

    def send_steal(self, indices) -> None:
        raise NotImplementedError("--dist worksteal is not supported by lanes")

    def shutdown(self) -> None:
        if not self._shutting_down:
            self._shutting_down = True
            if not self.wc.shutting_down:
                with contextlib.suppress(OSError):
                    self.wc.sendcommand("lanes_shutdown", lane=self.lane)

    def __repr__(self) -> str:
        return f"<LaneProxy {self.gateway.id}>"


class LaneMux:
    """Presents real xdist workers to DSession, and P x M lanes to the real scheduler."""

    def __init__(self, inner, lanes_per_worker: int, config) -> None:
        self.inner = inner
        self.m = lanes_per_worker
        self.config = config
        self.vnodes: dict = {}   # WorkerController -> [LaneProxy]
        self.owner: dict = {}    # item index -> LaneProxy that is running it
        inner.numnodes = self.total_lanes = inner.numnodes * lanes_per_worker

    # --- the DSession-facing surface (every attribute DSession reads) ---
    @property
    def nodes(self):
        return list(self.vnodes)

    @property
    def collection_is_completed(self):
        return self.inner.collection_is_completed

    @property
    def tests_finished(self):
        return self.inner.tests_finished

    @property
    def has_pending(self):
        return self.inner.has_pending

    def add_node(self, wc) -> None:
        lanes = self.vnodes[wc] = [LaneProxy(wc, i, self) for i in range(self.m)]
        for v in lanes:
            self.inner.add_node(v)

    def add_node_collection(self, wc, ids) -> None:
        for v in self.vnodes[wc]:
            self.inner.add_node_collection(v, ids)

    def schedule(self) -> None:
        self.inner.schedule()

    def mark_test_complete(self, wc, item_index, duration=0) -> None:
        self.inner.mark_test_complete(self.owner[item_index], item_index, duration)

    def remove_node(self, wc):
        """A worker process died: remove all its lanes from the scheduler."""
        lanes = self.vnodes.pop(wc)
        # All of them are dead: mark them first, or removing one lets the scheduler
        # reschedule its tests onto a dead sibling (send fails: INTERNALERROR, run over).
        for v in lanes:
            v.shutting_down = True
        live = set(self.inner.nodes)
        crashes = [c for c in (self.inner.remove_node(v) for v in lanes if v in live) if c]
        if len(crashes) > 1:
            # DSession reports one crashed item per worker; report the other lanes' ourselves.
            dsession = self.config.pluginmanager.get_plugin("dsession")
            for c in crashes[1:]:
                dsession.handle_crashitem(c, wc)                                     # X3
        return crashes[0] if crashes else None

    def remove_pending_tests_from_node(self, *a, **k):
        raise NotImplementedError("--dist worksteal is not supported by lanes")

    def __getattr__(self, name):
        return getattr(self.inner, name)


#: Interpreter flags lanes depend on, with the environment variable that sets each one
#: in a new interpreter. xdist starts its workers without the controller's -X options.
PROPAGATED_FLAGS = (("context_aware_warnings", "PYTHON_CONTEXT_AWARE_WARNINGS"),
                    ("thread_inherit_context", "PYTHON_THREAD_INHERIT_CONTEXT"))


def propagate_interpreter_flags() -> dict:
    """Set the environment so xdist's workers get the controller's flags.

    Returns the previous values, for ``restore_environment``. Without this,
    ``python -X context_aware_warnings=1 -m pytest -n 2 --lanes 4`` had every
    worker refuse to run: the flag reached the controller only.
    """
    previous = {}
    for flag, variable in PROPAGATED_FLAGS:
        if getattr(sys.flags, flag, False):
            previous[variable] = os.environ.get(variable)
            os.environ[variable] = "1"
    return previous


def restore_environment(previous: dict) -> None:
    for variable, value in previous.items():
        if value is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = value


class LanesController:
    """Plugin registered on the hybrid controller: wraps the scheduler in a LaneMux.

    It passes the controller's interpreter flags to the workers
    (``propagate_interpreter_flags``), and makes a worker's INTERNALERROR fail the run. xdist's controller prints
    it (via ``pytest_internalerror``) and carries on, and the run can still exit 0
    (xdist 3.8.0). A worker's lanes report a failed integrity check that way
    (integrity.py), and such a run must not look green.
    """

    def __init__(self, config) -> None:
        self.m = config.getoption("lanes")
        self.internal_error = False
        self._environment = propagate_interpreter_flags()

    def pytest_unconfigure(self, config):
        restore_environment(self._environment)

    def pytest_internalerror(self, excrepr, excinfo):
        self.internal_error = True

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session):
        if self.internal_error:
            session.exitstatus = pytest.ExitCode.INTERNAL_ERROR

    @pytest.hookimpl(wrapper=True)
    def pytest_xdist_make_scheduler(self, config, log):
        sched = yield                       # whatever xdist or your conftest returned
        reject_unsupported(sched)
        return LaneMux(sched, self.m, config)

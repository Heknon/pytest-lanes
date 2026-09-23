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
        self.shutting_down = False

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
        if not self.shutting_down:
            self.shutting_down = True
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


class LanesController:
    """Plugin registered on the hybrid controller: wraps the scheduler in a LaneMux."""

    def __init__(self, config) -> None:
        self.m = config.getoption("lanes")

    @pytest.hookimpl(wrapper=True)
    def pytest_xdist_make_scheduler(self, config, log):
        sched = yield                       # whatever xdist or your conftest returned
        reject_unsupported(sched)
        return LaneMux(sched, self.m, config)

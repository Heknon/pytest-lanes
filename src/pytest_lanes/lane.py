"""A lane: one thread standing in for one xdist worker.

``ThreadNode`` holds both halves of what xdist splits across a process boundary:

* toward the scheduler, the members of xdist's ``WorkerController`` that the
  ``load*`` schedulers use: ``gateway``, ``shutting_down``,
  ``send_runtest_some()`` and ``shutdown()``;
* toward test execution, the state a worker process would own privately: its
  ``SetupState``, fixture caches, captured stdout/stderr and log handlers.

Code running on a lane finds its node through the ``LANE`` contextvar. On the
main thread, ``LANE.get()`` is ``None``, and everything behaves as plain pytest.
"""
from __future__ import annotations

import contextvars
import io
import os
import platform
import queue
import sys
from types import SimpleNamespace

LANE: contextvars.ContextVar = contextvars.ContextVar("pytest_lanes.lane", default=None)

#: Queue marker that ends a lane's loop, like xdist's "shutdown" command.
SHUTDOWN = object()


class _Gateway:
    """The parts of an execnet gateway that schedulers and reporters read."""

    def __init__(self, id_: str) -> None:
        self.id = id_
        self.spec = SimpleNamespace(id=id_, popen=False, execmodel="thread")


class ThreadNode:
    """A lane: an xdist WorkerController look-alike plus its private execution state."""

    def __init__(self, id_: str, count: int, uid: str, setupstate, log_handlers: dict) -> None:
        self.gateway = _Gateway(id_)
        self.workerinput = {"workerid": id_, "workercount": count, "testrunuid": uid}
        self.workeroutput: dict = {}
        # Same keys as xdist.remote.getinfodict(); pytest's reports.getworkerinfoline reads them.
        self.workerinfo = {
            "id": id_, "spec": self.gateway.spec, "version": sys.version,
            "version_info": tuple(sys.version_info), "sysplatform": sys.platform,
            "platform": platform.platform(), "executable": sys.executable, "cwd": os.getcwd(),
        }

        # Scheduler-facing: item indices to run, then SHUTDOWN.
        self.shutting_down = False
        self.queue: queue.Queue = queue.Queue()

        # Execution-facing: what a worker process would own.
        self.setupstate = setupstate
        self.fixture_state: dict = {}
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.log_handlers = log_handlers

    def send_runtest_some(self, indices) -> None:
        for i in indices:
            self.queue.put(i)

    def send_runtest_all(self) -> None:  # used only by --dist each
        raise NotImplementedError("--dist each is not supported by lanes")

    def send_steal(self, indices) -> None:  # used only by --dist worksteal
        raise NotImplementedError("--dist worksteal is not supported by lanes")

    def shutdown(self) -> None:
        if not self.shutting_down:
            self.shutting_down = True
            self.queue.put(SHUTDOWN)

    def __repr__(self) -> str:
        return f"<ThreadNode {self.gateway.id}>"

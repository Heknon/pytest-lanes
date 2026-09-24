"""A lane: one thread standing in for one xdist worker.

``ThreadNode`` holds both halves of what xdist splits across a process boundary:

* toward the scheduler, the members of xdist's ``WorkerController`` that the
  ``load*`` schedulers use: ``gateway``, ``shutting_down``,
  ``send_runtest_some()`` and ``shutdown()``;
* toward test execution, the state a worker process would own privately: its
  ``SetupState``, fixture caches, captured stdout/stderr, log handlers and
  basetemp.

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
import weakref
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

    def __init__(self, id_: str, workerinput: dict, setupstate, log_handlers: dict) -> None:
        self.gateway = _Gateway(id_)
        # What config.workerinput / workeroutput are on this lane (isolation.py, P10), so
        # worker_id, testrun_uid and xdist.get_xdist_worker_id() name this lane.
        self.workerinput = workerinput
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
        # Per-lane FixtureDef state (isolation.py, P2), keyed weakly: pytest 9 makes a
        # FixtureDef per test (for `request`), and strong keys kept every one alive.
        self.fixture_state = weakref.WeakKeyDictionary()   # FixtureDef -> [cached_result, finalizers]
        self.fixture_attrs = weakref.WeakKeyDictionary()   # FixtureDef -> {attr: value}
        self.fixture_scopes: list = []  # scopes of the fixtures being set up now (patch guard)
        # This lane's own values of PER_LANE_ENVIRON keys, as os.environ shows them on the
        # lane (isolation.py, P6); None: deleted. A key not here has its default.
        self.environ: dict = {}
        self.out = io.StringIO()
        self.err = io.StringIO()
        # contextlib.redirect_stdout/stderr targets entered on this lane, innermost last
        # (capture.py, P13): the lane's writes go there instead of its buffers.
        self.redirects: dict = {"out": [], "err": []}
        # sys.stdout.fileno() on this lane: a capture file per stream, read into the
        # buffers after each phase (capture.py), as pytest's fd capture would.
        self.fd_files: dict = {}
        # bytes written to sys.stdout.buffer: decoded incrementally, so a character
        # split across writes is not mangled.
        self.decoders: dict = {}
        self.log_handlers = log_handlers
        self.tmp_path_factory = None  # this lane's basetemp, created on first use (isolation.py)
        self.current_item = None      # the item this lane is running, if any

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

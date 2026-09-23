"""The execution engine shared by every process that runs tests on lanes.

``LaneRunner`` is the base of ``SingleProcessSession`` (``--lanes N``) and
``HybridWorkerSession`` (a worker of ``-n P --lanes M``). It owns:

* setup and teardown of lane isolation and hook routing, for the whole session;
* per-phase output capture;
* the lane loop (``_node_loop``), one thread per ``ThreadNode``, which follows
  ``xdist.remote.WorkerInteractor``: look one item ahead so that ``nextitem``
  drives teardown, and stop at SHUTDOWN;
* the main-thread event loop (``_pump``), which replays routed hooks and tells
  the controller side about each finished item;
* the exclusivity lock: tests using capsys, capfd, recwarn (or marked
  ``lanes_exclusive``) run alone within their process.
"""
from __future__ import annotations

import contextlib
import queue
import threading
import time
import uuid
from typing import NamedTuple

import pytest

from .capture import capture_phase
from .hookrouting import ControllerHookRouter, HookCall
from .isolation import isolate_lanes
from .lane import LANE, SHUTDOWN, ThreadNode


class ItemDone(NamedTuple):
    """Queued by a lane after each item; the lane waits on ``ack`` before continuing."""

    node: ThreadNode
    index: int
    duration: float
    ack: threading.Event


class LaneRunner:
    #: See ControllerHookRouter: False where reports must stay serializable.
    set_report_node = True

    def __init__(self, config) -> None:
        self.config = config
        self.n = config.getoption("lanes")
        self.uid = uuid.uuid4().hex
        self.events: queue.Queue = queue.Queue()   # HookCall | ItemDone, consumed by _pump
        self.stop = threading.Event()              # -x / --maxfail reached
        self.errors: list = []                     # exceptions escaping a lane thread
        self.exclusive_lock = ReadWriteLock()
        self._session_stack = contextlib.ExitStack()

    # ---- session-long install / uninstall -------------------------------------
    @pytest.hookimpl(trylast=True)  # after runner.py creates session._setupstate
    def pytest_sessionstart(self, session):
        stack = self._session_stack
        self.lane_state = stack.enter_context(isolate_lanes(self.config, session))
        self.hooks = stack.enter_context(ControllerHookRouter(
            self.config.pluginmanager, self.events, set_report_node=self.set_report_node))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session):
        self._session_stack.close()

    # ---- per-phase capture (same nesting as CaptureManager) -------------------
    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_setup(self, item):
        return (yield from capture_phase(item, "setup"))

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_call(self, item):
        return (yield from capture_phase(item, "call"))

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_teardown(self, item):
        return (yield from capture_phase(item, "teardown"))

    # ---- lanes ------------------------------------------------------------------
    def new_node(self, id_: str) -> ThreadNode:
        return ThreadNode(id_, self.n, self.uid, setupstate=self.lane_state.setupstate(),
                          log_handlers=self.lane_state.log_handlers())

    def is_exclusive(self, item) -> bool:
        fixtures = set(self.config.getini("lanes_exclusive_fixtures"))
        return bool(item.get_closest_marker("lanes_exclusive")
                    or fixtures & set(getattr(item, "fixturenames", ())))

    def start(self, node: ThreadNode, items) -> threading.Thread:
        def run():
            try:
                self._node_loop(node, items)
            except BaseException as e:  # re-raised on the main thread by _pump
                self.errors.append(e)

        t = threading.Thread(target=run, name=f"lane-{node.gateway.id}", daemon=True)
        t.start()
        return t

    def _node_loop(self, node: ThreadNode, items) -> None:
        token = LANE.set(node)
        hook = self.config.hook
        try:
            nxt = node.queue.get()
            while nxt is not SHUTDOWN:
                index = nxt
                nxt = node.queue.get()          # lookahead decides nextitem (teardown scope)
                if self.stop.is_set():
                    break
                item = items[index]
                nextitem = None if nxt is SHUTDOWN else items[nxt]
                start = time.perf_counter()
                rw = self.exclusive_lock
                with (rw.exclusive if self.is_exclusive(item) else rw.shared)():
                    hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
                # Wait until the main thread has replayed this item's reports and
                # told the scheduler, so -x/--maxfail stop exactly as a sequential run does.
                ack = threading.Event()
                self.events.put(ItemDone(node, index, time.perf_counter() - start, ack))
                ack.wait()
                if self.stop.is_set():
                    break
            node.setupstate.teardown_exact(None)
        finally:
            LANE.reset(token)

    def _pump(self, threads, sched, session, nodes, on_done=None, before_replay=None) -> None:
        """Main-thread event loop until every lane thread has exited.

        ``sched``: the scheduler to update on each ItemDone (None in a hybrid worker,
        whose scheduler lives in the controller). ``on_done(node, index, duration)``
        and ``before_replay(call)`` are the hybrid worker's hooks into the loop.
        """
        while True:
            try:
                event = self.events.get(timeout=0.05)
            except queue.Empty:
                if not any(t.is_alive() for t in threads) and self.events.empty():
                    break
                continue
            if isinstance(event, HookCall):
                if before_replay is not None:
                    before_replay(event)
                self.hooks.replay(event)
                continue
            if on_done is not None:
                on_done(event.node, event.index, event.duration)
            if sched is not None:
                sched.mark_test_complete(event.node, event.index, event.duration)
                if sched.tests_finished:
                    for n in nodes:
                        n.shutdown()
            if session.shouldfail or session.shouldstop:
                self.stop.set()
                for n in nodes:
                    n.shutdown()
            event.ack.set()
        for t in threads:
            t.join()
        if self.errors:
            raise self.errors[0]


class ReadWriteLock:
    """Writer-preferring: an exclusive test waits for running tests, then runs alone."""

    def __init__(self) -> None:
        self._c = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting = 0

    @contextlib.contextmanager
    def shared(self):
        with self._c:
            while self._writer or self._waiting:
                self._c.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._c:
                self._readers -= 1
                self._c.notify_all()

    @contextlib.contextmanager
    def exclusive(self):
        with self._c:
            self._waiting += 1
            while self._writer or self._readers:
                self._c.wait()
            self._waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._c:
                self._writer = False
                self._c.notify_all()

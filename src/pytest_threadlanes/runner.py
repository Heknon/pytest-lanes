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
* Ctrl-C (``_interrupt``): the running lanes get ``KeyboardInterrupt`` too, tear
  down their fixtures on their own thread, as pytest does after an interrupt, and
  are waited for up to the ``lanes_interrupt_grace`` ini (a second Ctrl-C stops
  waiting). A lane blocked in one long C call cannot be interrupted, and is named;
* the exclusivity lock: tests that swap process-wide streams or warning state
  run alone within their process. That is tests using capsys, capfd, recwarn and
  pytest-cov's no_cover (the ``lanes_exclusive_fixtures`` ini), marked
  ``lanes_exclusive`` or ``no_cover`` (pytest-cov pauses coverage for the whole
  process: tests on other lanes went unmeasured), and doctests,
  whose runner swaps ``sys.stdout``. A capture fixture requested at run time by a
  test that is not exclusive fails that test with instructions.
"""
from __future__ import annotations

import contextlib
import io
import queue
import sys
import threading
import time
import uuid
from typing import NamedTuple

import pytest

from .capture import capture_phase
from .hookrouting import ControllerHookRouter, HookCall
from .integrity import Ledger, StdioWatch
from .isolation import isolate_lanes, show_unmatched_warnings_always
from .lane import LANE, SHUTDOWN, ThreadNode


def is_doctest(item) -> bool:
    """P9: doctest items, whose runner swaps sys.stdout for the whole process."""
    try:
        from _pytest.doctest import DoctestItem
    except ImportError:  # pragma: no cover - the probe fails first
        return False
    return isinstance(item, DoctestItem)


class ItemDone(NamedTuple):
    """Queued by a lane after each item; the lane waits on ``ack`` before continuing."""

    node: ThreadNode
    index: int
    nodeid: str
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
        self.teardown_errors: list = []            # (lane id, error) from teardown after a stop
        self.exclusive_lock = ReadWriteLock()
        self.interrupting = False                  # Ctrl-C: _interrupt is stopping the lanes
        self._pump_state = None                    # set by _pump_events
        self.ledger = Ledger()                     # run-time integrity check (integrity.py)
        self.nodes: list = []                      # every lane of this process, as created
        self._session_stack = contextlib.ExitStack()
        grace = config.getini("lanes_interrupt_grace")
        try:
            self.grace = float(grace)
            if self.grace < 0:
                raise ValueError
        except ValueError:
            raise pytest.UsageError(f"lanes_interrupt_grace must be a number of seconds >= 0, "
                                    f"not {grace!r}") from None

    # ---- session-long install / uninstall -------------------------------------
    @pytest.hookimpl(trylast=True)  # after runner.py creates session._setupstate
    def pytest_sessionstart(self, session):
        stack = self._session_stack
        try:
            self.lane_state = stack.enter_context(isolate_lanes(self.config, session, self.is_exclusive))
            self.hooks = stack.enter_context(ControllerHookRouter(
                self.config.pluginmanager, self.events, session, set_report_node=self.set_report_node,
                ledger=self.ledger))
            watch = StdioWatch(self.ledger, self.nodes, lambda: self.exclusive_lock.exclusive_active,
                               redirects_per_lane=self.config.getoption("capture") != "no")
            watch.start()
            stack.callback(watch.stop)
        except BaseException:
            stack.close()       # pytest skips sessionfinish when sessionstart fails
            raise

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_make_collect_report(self, collector):
        """Output printed while a file is collected goes into its collect report, as
        pytest's capture does (lanes turn that off). Collection runs before any lane
        starts, so replacing sys.stdout for the process is safe here."""
        if self.config.getoption("capture") == "no" or not isinstance(collector, pytest.File):
            return (yield)
        out, err = io.StringIO(), io.StringIO()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            report = yield
        finally:
            sys.stdout, sys.stderr = saved
        for title, buf in (("Captured stdout", out), ("Captured stderr", err)):
            if buf.getvalue():
                report.sections.append((title, buf.getvalue()))
        return report

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session):
        self._session_stack.close()

    # ---- per-phase capture (same nesting as CaptureManager) -------------------
    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_setup(self, item):
        show_unmatched_warnings_always()
        return (yield from capture_phase(item, "setup"))

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_call(self, item):
        show_unmatched_warnings_always()
        return (yield from capture_phase(item, "call"))

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_teardown(self, item):
        show_unmatched_warnings_always()
        return (yield from capture_phase(item, "teardown"))

    # ---- lanes ------------------------------------------------------------------
    def new_node(self, id_: str) -> ThreadNode:
        node = ThreadNode(id_, self.lane_workerinput(id_), setupstate=self.lane_state.setupstate(),
                          log_handlers=self.lane_state.log_handlers())
        self.nodes.append(node)
        return node

    def lane_workerinput(self, id_: str) -> dict:
        """A lane's workerinput, shaped like the one xdist hands a worker."""
        return {"workerid": id_, "workercount": self.n, "testrunuid": self.uid,
                "mainargv": list(sys.argv)}

    def is_exclusive(self, item) -> bool:
        fixtures = set(self.config.getini("lanes_exclusive_fixtures"))
        return bool(item.get_closest_marker("lanes_exclusive")
                    or item.get_closest_marker("no_cover")   # pytest-cov: see DEFAULT_EXCLUSIVE
                    or fixtures & set(getattr(item, "fixturenames", ()))
                    or is_doctest(item))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        """Fail closed when an exclusive fixture is requested only at run time; and note
        the scope of the fixture being set up, for the patch guard (isolation.py, P14)."""
        lane = LANE.get()
        if lane is None:
            return (yield)
        # The running test, not request.node: for a module or session fixture that is the
        # Module or Session, which never counts as exclusive.
        item = lane.current_item or request.node
        if fixturedef.argname in self.config.getini("lanes_exclusive_fixtures") \
                and not self.is_exclusive(item):
            pytest.fail(f"pytest-threadlanes: {fixturedef.argname!r} was requested at run time "
                        f"(getfixturevalue), so this test was not scheduled to run alone and "
                        f"would capture other lanes' output. Add it to the test's arguments "
                        f"or mark the test @pytest.mark.lanes_exclusive.", pytrace=False)
        lane.fixture_scopes.append(getattr(fixturedef, "scope", "function"))
        try:
            return (yield)
        finally:
            lane.fixture_scopes.pop()

    def start(self, node: ThreadNode, items) -> threading.Thread:
        def run():
            try:
                self._node_loop(node, items)
            except KeyboardInterrupt as e:  # torn down already (_node_loop)
                if not self.interrupting:     # raised by the test itself: end the run, as pytest
                    self.errors.append(e)
            except BaseException as e:  # re-raised on the main thread by _pump
                self.errors.append(e)

        t = threading.Thread(target=run, name=f"lane-{node.gateway.id}", daemon=True)
        t.start()
        return t

    def stopping(self, session) -> bool:
        return self.stop.is_set() or bool(session.shouldfail or session.shouldstop)

    def _node_loop(self, node: ThreadNode, items) -> None:
        token = LANE.set(node)
        hook = self.config.hook
        session = items[0].session if items else None
        try:
            nxt = node.queue.get()
            while nxt is not SHUTDOWN:
                index = nxt
                nxt = node.queue.get()          # lookahead decides nextitem (teardown scope)
                if self.stopping(session):
                    break
                item = items[index]
                nextitem = None if nxt is SHUTDOWN else items[nxt]
                rw = self.exclusive_lock
                with (rw.exclusive if self.is_exclusive(item) else rw.shared)():
                    if self.stopping(session):   # -x while this lane waited for the lock
                        break
                    start = time.perf_counter()  # as xdist: the protocol, not the wait for the lock
                    node.current_index = index   # until the next item: its reports may replay later
                    node.current_item = item     # running from here: not while waiting for the lock
                    try:
                        hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
                    finally:
                        node.current_item = None
                    # Wait until the main thread has replayed this item's reports and told
                    # the scheduler, so -x/--maxfail stop exactly as a sequential run does.
                    # Still holding the lock: an exclusive test (hybrid mode runs them
                    # between others) must not start while these reports are replayed,
                    # or its capfd captures what the replay writes.
                    ack = threading.Event()
                    self.events.put(ItemDone(node, index, item.nodeid, time.perf_counter() - start, ack))
                    ack.wait()
                if self.stopping(session):      # as xdist's worker loop, after each item
                    break
            with self.exclusive_lock.shared():  # not beside an exclusive test (capfd)
                self._final_teardown(node)
        except BaseException:
            # Ctrl-C, pytest.exit(), or an error out of the protocol: as pytest's own
            # sessionfinish would, tear down what this lane holds before leaving.
            node.current_item = None
            self._final_teardown(node)
            raise
        finally:
            for f in node.fd_files.values():   # the lane's fd capture files (capture.py)
                f.close()
            node.fd_files.clear()
            LANE.reset(token)

    def _final_teardown(self, node: ThreadNode) -> None:
        """Tear down what the lane's last item left for its successor.

        Normally a no-op: the last item ran with nextitem=None, or pytest tore it down
        fully because the session was stopping. Only a lane that was between items
        when the run stopped still holds fixtures; their errors are reported after
        the run (``report_teardown_errors``), as plain pytest reports errors from its
        own end-of-session teardown, instead of killing the lane as an INTERNALERROR.
        """
        try:
            node.setupstate.teardown_exact(None)
        except Exception as e:
            self.teardown_errors.append((node.gateway.id, e))

    def report_teardown_errors(self) -> None:
        for lane_id, error in self.teardown_errors:
            self._say(f"ERROR tearing down lane {lane_id} after the run stopped: "
                      f"{type(error).__name__}: {error}", red=True)

    def _report_after_interrupt(self) -> None:
        """What the lanes hit while being stopped: nothing may vanish silently."""
        self.report_teardown_errors()
        self.teardown_errors.clear()
        for error in self.errors:
            if not isinstance(error, KeyboardInterrupt):
                self._say(f"ERROR in a lane while stopping: {type(error).__name__}: {error}", red=True)

    def _pump(self, threads, sched, session, nodes, on_done=None, before_replay=None) -> None:
        """Main-thread event loop until every lane thread has exited.

        ``sched``: the scheduler to update on each ItemDone (None in a hybrid worker,
        whose scheduler lives in the controller). ``on_done(node, index, duration)``
        and ``before_replay(call)`` are the hybrid worker's hooks into the loop.
        """
        try:
            self._pump_events(threads, sched, session, nodes, on_done, before_replay)
        except KeyboardInterrupt:
            self._interrupt(threads, nodes)
            self._report_after_interrupt()
            raise
        except BaseException:
            # A main-thread hook failed (a plugin's logreport, a custom scheduler): the run
            # ends with INTERNALERROR, but the lanes still tear down, as xdist's workers do.
            self._interrupt(threads, nodes, reason="Internal error")
            self._report_after_interrupt()
            raise
        for t in threads:
            t.join()
        self.report_teardown_errors()
        self.teardown_errors.clear()
        if self.errors:
            raise self.errors[0]
        self.ledger.raise_if_violated()

    def _pump_events(self, threads, sched, session, nodes, on_done, before_replay) -> None:
        # Kept on self for _interrupt, which drains the queue after Ctrl-C with the same
        # handling: a finished item must still reach the scheduler (or, in hybrid mode,
        # the controller calls it crashed).
        self._pump_state = _PumpState(sched, session, nodes, on_done, before_replay)
        while True:
            try:
                event = self.events.get(timeout=0.05)
            except queue.Empty:
                self._check_lane_errors(nodes)
                if not any(t.is_alive() for t in threads) and self.events.empty():
                    break
                continue
            # Lane errors are checked only after an event is handled: an event taken off
            # the queue and then dropped (an unacknowledged ItemDone) stranded its lane.
            self._handle(event)
            self._check_lane_errors(nodes)

    def _handle(self, event) -> None:
        """One event from a lane: replay (or hold) a hook call, or finish an item.
        Never leaves a lane waiting for its acknowledgement."""
        st = self._pump_state
        if isinstance(event, HookCall):
            # An exclusive test may redirect fd 1/2 for the process (capfd): output the
            # main thread wrote meanwhile (the reporters') went into the test's capture.
            # Its hook calls are held, in order, and replayed once it is done, as xdist's
            # controller prints a worker's reports in another process.
            node = st.by_id.get(event.lane)
            item = node.current_item if node is not None else None
            if event.lane in st.held or (item is not None and self.is_exclusive(item)):
                st.held.setdefault(event.lane, []).append(event)
            else:
                self._replay(event, st.before_replay)
            return
        try:
            self._item_done(event, st.held, st.sched, st.session, st.nodes, st.on_done, st.before_replay)
        finally:                                  # even on Ctrl-C: the lane waits for this
            event.ack.set()

    def _check_lane_errors(self, nodes) -> None:
        """A lane died: stop the others now, as xdist's controller does on a worker error.
        A KeyboardInterrupt raised by a test interrupts the run, like Ctrl-C."""
        if not self.errors:
            return
        if any(isinstance(e, KeyboardInterrupt) for e in self.errors):
            self.errors[:] = [e for e in self.errors if not isinstance(e, KeyboardInterrupt)]
            raise KeyboardInterrupt
        if not self.stop.is_set():
            self.stop.set()
            for n in nodes:
                n.shutdown()

    def _item_done(self, event, held, sched, session, nodes, on_done, before_replay) -> None:
        for call in held.pop(event.node.gateway.id, ()):
            self._replay(call, before_replay)
        self.ledger.item_done(event.node.gateway.id, event.nodeid)
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

    def _replay(self, call: HookCall, before_replay) -> None:
        self.ledger.replayed(call.lane, call.name, call.kwargs)
        if before_replay is not None:
            before_replay(call)
        self.hooks.replay(call)

    def _interrupt(self, threads, nodes, reason: str = "Interrupted") -> None:
        """Ctrl-C (or a main-thread error): interrupt the lanes, let them tear down, wait."""
        self.interrupting = True
        self.stop.set()
        for n in nodes:
            n.shutdown()
        running = {t: n for t, n in zip(threads, nodes) if t.is_alive()}
        for t in running:
            raise_in_thread(t, KeyboardInterrupt)
        grace = self.grace
        deadline = time.monotonic() + grace
        self._say(f"{reason}: stopping {len(running)} lane(s) and running their teardown "
                  f"(up to {grace:g}s; press Ctrl-C again to stop waiting)")
        try:
            while any(t.is_alive() for t in running) and time.monotonic() < deadline:
                try:
                    event = self.events.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    self._handle(event)        # reports of tests that did finish
                except Exception as e:         # keep draining: lanes wait for their acks
                    self._say(f"error while stopping: {type(e).__name__}: {e}")
        except KeyboardInterrupt:
            pass
        left = [(n.gateway.id, n.current_item) for t, n in running.items() if t.is_alive()]
        for lane_id, item in left:
            where = f"{item.nodeid} was left without teardown" if item is not None \
                else "its last fixtures were left without teardown"
            self._say(f"lane {lane_id} did not stop: {where} "
                      f"(blocked in a call that cannot be interrupted)")

    def _say(self, line: str, red: bool = False) -> None:
        tr = self.config.pluginmanager.get_plugin("terminalreporter")
        if tr is not None:
            tr.write_line(line, red=red, yellow=not red)
        else:
            sys.stderr.write(line + "\n")


class _PumpState:
    """What the main-thread loop needs to handle an event (``LaneRunner._handle``)."""

    def __init__(self, sched, session, nodes, on_done, before_replay) -> None:
        self.sched, self.session, self.nodes = sched, session, nodes
        self.on_done, self.before_replay = on_done, before_replay
        self.by_id = {n.gateway.id: n for n in nodes}
        self.held: dict = {}                    # lane id -> hook calls held (exclusive test)


def raise_in_thread(thread: threading.Thread, exc_type) -> bool:
    """Raise ``exc_type`` in ``thread`` when it next runs Python code (CPython C API).

    Best effort: a thread blocked in one long C call (a sleep, a socket read without
    a timeout) only sees it once that call returns.
    """
    import ctypes

    if thread.ident is None:
        return False
    set_async_exc = ctypes.pythonapi.PyThreadState_SetAsyncExc
    set_async_exc.argtypes = (ctypes.c_ulong, ctypes.py_object)
    return set_async_exc(thread.ident, exc_type) == 1


class ReadWriteLock:
    """Writer-preferring: an exclusive test waits for running tests, then runs alone."""

    def __init__(self) -> None:
        self._c = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting = 0

    @property
    def exclusive_active(self) -> bool:
        """An exclusive test holds the lock: it runs alone (other lanes wait)."""
        return self._writer

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

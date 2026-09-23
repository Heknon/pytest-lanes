"""pytest-lanes (prototype): drive pytest-xdist's schedulers with thread workers.

One scheduler definition, two execution backends:

    def pytest_xdist_make_scheduler(config, log):      # conftest.py
        return EnvScheduling(config, log)              # your LoadScope subclass

    pytest -n 16            # xdist: processes
    pytest --lanes 400      # lanes: threads in this process, same scheduler

Each lane is a ThreadNode that duck-types the 4 members xdist schedulers use
(gateway, shutting_down, send_runtest_some, shutdown) and runs items with the
same loop as xdist.remote.WorkerInteractor (lookahead nextitem, SHUTDOWN marker).
Controller-side behaviour mirrors xdist.dsession.DSession: add_node,
add_node_collection, schedule, mark_test_complete, shutdown on tests_finished.

Private touchpoints (probed at startup; fail closed):
  P1 session._setupstate                          (pytest)
  P2 FixtureDef.cached_result / ._finalizers      (pytest)
  P3 LoggingPlugin.caplog_handler/.report_handler (pytest)
  P4 PluginManager._inner_hookexec                (pluggy)
  P5 item._nodeid "@group" suffix, loadgroup only (same thing xdist's worker does)
  P6 _pytest.runner._update_current_test_var (PYTEST_CURRENT_TEST pop race)
  X1 xdist scheduler protocol (xdist, semi-public; probed per scheduler)
  hybrid (-n N --lanes M) only:
  X2 WorkerInteractor.channel / sendevent on the worker (xdist private)
  X3 DSession.handle_crashitem for 2nd+ crashed lanes of one worker (xdist private)
"""
from __future__ import annotations

import contextlib
import contextvars
import io
import logging
import os
import platform
import queue
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

_LANE: contextvars.ContextVar = contextvars.ContextVar("pytest_lanes.lane", default=None)
_SHUTDOWN = object()

# Exactly the hooks xdist forwards worker -> controller.
ROUTED = frozenset({
    "pytest_runtest_logstart",
    "pytest_runtest_logreport",
    "pytest_runtest_logfinish",
    "pytest_warning_recorded",
})
DEFAULT_EXCLUSIVE = ("capsys", "capsysbinary", "capfd", "capfdbinary", "capteesys", "recwarn")
SUPPORTED_DIST = ("load", "loadscope", "loadfile", "loadgroup")


# --------------------------------------------------------------------------- nodes
class _Gateway:
    def __init__(self, id_: str) -> None:
        self.id = id_
        self.spec = SimpleNamespace(id=id_, popen=False, execmodel="thread")


class ThreadNode:
    """A lane: xdist WorkerController look-alike + per-lane execution state."""

    def __init__(self, id_, count, uid, setupstate_cls, log_templates) -> None:
        self.gateway = _Gateway(id_)
        self.workerinput = {"workerid": id_, "workercount": count, "testrunuid": uid}
        self.workeroutput: dict = {}
        # Same keys xdist.remote.getinfodict() sends; read by pytest's reports.getworkerinfoline.
        self.workerinfo = {
            "id": id_, "spec": self.gateway.spec, "version": sys.version,
            "version_info": tuple(sys.version_info), "sysplatform": sys.platform,
            "platform": platform.platform(), "executable": sys.executable, "cwd": os.getcwd(),
        }
        # --- scheduler-facing protocol ---
        self.shutting_down = False
        self.queue: queue.Queue = queue.Queue()
        # --- execution state (what a worker process would own) ---
        self.setupstate = setupstate_cls()
        self.fixture_state: dict = {}
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.log_handlers = {}
        for key, tmpl in log_templates.items():
            h = type(tmpl)()
            h.setFormatter(tmpl.formatter)
            h.setLevel(tmpl.level)
            self.log_handlers[key] = h

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
            self.queue.put(_SHUTDOWN)

    def __repr__(self) -> str:
        return f"<ThreadNode {self.gateway.id}>"


# --------------------------------------------------------------- P1: setup state
class _SetupStateRouter:
    def __init__(self, main) -> None:
        object.__setattr__(self, "_main", main)

    def _target(self):
        lane = _LANE.get()
        return self._main if lane is None else lane.setupstate

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        setattr(self._target(), name, value)


# ------------------------------------------------------------ P2: fixture caches
def _patch_fixturedef(FixtureDef) -> None:
    if FixtureDef.__dict__.get("_lanes_patched"):
        return

    def slot(self):
        lane = _LANE.get()
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


# ---------------------------------------------------------------- P3: logging
class _LogDispatch(logging.Handler):
    """Stands in for LoggingPlugin.caplog_handler / report_handler.

    catching_logs() may attach/detach it concurrently; it never emits in lanes
    (the permanent _LogRouter does), so add/remove races are harmless.
    """

    def __init__(self, key: str, fallback) -> None:
        self.__dict__["_key"] = key
        self.__dict__["_fallback"] = fallback
        super().__init__()

    def _t(self):
        lane = _LANE.get()
        return self.__dict__["_fallback"] if lane is None else lane.log_handlers[self.__dict__["_key"]]

    level = property(lambda s: s._t().level, lambda s, v: None)
    records = property(lambda s: s._t().records)
    stream = property(lambda s: s._t().stream)
    formatter = property(lambda s: s._t().formatter, lambda s, v: None)

    def setLevel(self, level):
        self._t().setLevel(level)

    def reset(self):
        self._t().reset()

    def clear(self):
        self._t().clear()

    def handle(self, record):
        if _LANE.get() is None:
            return self.__dict__["_fallback"].handle(record)
        return False

    def emit(self, record):  # pragma: no cover - handle() short-circuits
        pass


class _LogRouter(logging.Handler):
    def handle(self, record):
        lane = _LANE.get()
        if lane is None:
            return False
        for h in lane.log_handlers.values():
            if record.levelno >= h.level:
                h.handle(record)
        return True

    def emit(self, record):  # pragma: no cover
        pass


# ------------------------------------------------------------------ capture
class _LaneStream(io.TextIOBase):
    def __init__(self, real, attr: str) -> None:
        self._real = real
        self._attr = attr

    def write(self, s):
        lane = _LANE.get()
        if lane is None:
            return self._real.write(s)
        return getattr(lane, self._attr).write(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


# ------------------------------------------------------------------- plugin
def pytest_addoption(parser):
    g = parser.getgroup("lanes")
    g.addoption("--lanes", type=int, default=None, metavar="N",
                help="run tests on N thread lanes in this process, scheduled by pytest-xdist schedulers")
    g.addoption("--lanes-dist", default="load", choices=SUPPORTED_DIST,
                help="built-in xdist scheduler to use when no pytest_xdist_make_scheduler "
                     "implementation returns one (default: load, like xdist)")
    g.addoption("--lanes-xdist-node-hooks", action="store_true", default=False,
                help="fire xdist node hooks (testnodeready/down) to plugins in lanes_node_hook_plugins")
    parser.addini("lanes_node_hook_plugins", "plugin names (substring match) that receive xdist "
                  "node hooks; others never see them", type="args", default=["conftest"])
    parser.addini("lanes_exclusive_fixtures", "fixtures forcing a test into the serial phase",
                  type="args", default=list(DEFAULT_EXCLUSIVE))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    ns = early_config.known_args_namespace
    if getattr(ns, "lanes", None):
        ns.capture = "no"  # lanes do their own per-test capture
    return (yield)


def pytest_configure(config):
    config.addinivalue_line("markers", "lanes_exclusive: run in the serial phase, alone")
    if config.getoption("lanes"):
        if not config.pluginmanager.hasplugin("xdist"):
            raise pytest.UsageError("--lanes drives pytest-xdist's schedulers; install pytest-xdist")
        if getattr(config.option, "usepdb", False):
            raise pytest.UsageError("--lanes is incompatible with --pdb (as is xdist)")
        if hasattr(config, "workerinput"):
            # Hybrid, worker side: this xdist worker process runs N thread lanes.
            _probe(config)
            config.pluginmanager.register(LanesSession(config, worker_mode=True), "lanes-session")
        elif getattr(config.option, "numprocesses", None) or getattr(config.option, "tx", None):
            # Hybrid, controller side: real xdist DSession + processes; lanes are virtual nodes.
            config.pluginmanager.register(LanesController(config), "lanes-controller")
        else:
            _probe(config)
            config.pluginmanager.register(LanesSession(config), "lanes-session")


def _probe(config) -> None:
    from _pytest.fixtures import FixtureDef  # noqa: PLC0415  (P2)

    problems = []
    if not hasattr(config.pluginmanager, "_inner_hookexec"):
        problems.append("P4 pluggy PluginManager._inner_hookexec")
    src = FixtureDef.__init__.__code__.co_names
    if not FixtureDef.__dict__.get("_lanes_patched") and not {"cached_result", "_finalizers"} <= set(src):
        problems.append("P2 FixtureDef.cached_result/_finalizers")
    if getattr(FixtureDef, "__slots__", None):
        problems.append("P2 FixtureDef now uses __slots__")
    lp = config.pluginmanager.get_plugin("logging-plugin")
    if lp is not None and not (hasattr(lp, "caplog_handler") and hasattr(lp, "report_handler")):
        problems.append("P3 LoggingPlugin handler attributes")
    wp = config.pluginmanager.get_plugin("warnings")
    if wp is not None and not getattr(sys.flags, "context_aware_warnings", False):
        problems.append("warnings plugin active but catch_warnings is not thread-safe here: "
                        "use Python>=3.14 with -X context_aware_warnings=1, or -p no:warnings")
    from _pytest import runner as _runner  # noqa: PLC0415

    if not callable(getattr(_runner, "_update_current_test_var", None)):
        problems.append("P6 _pytest.runner._update_current_test_var")
    for name, hook in (("threadexception", "threading.excepthook"),
                       ("unraisableexception", "sys.unraisablehook")):
        mod = config.pluginmanager.get_plugin(name)
        if mod is not None and not hasattr(mod, "pytest_configure"):
            problems.append(f"this pytest's {name} plugin swaps {hook} per test (races across lanes): "
                            f"upgrade pytest or pass -p no:{name}")
    try:
        from xdist.remote import Producer  # noqa: F401, PLC0415
        from xdist.scheduler import LoadScopeScheduling  # noqa: F401, PLC0415
    except ImportError as e:
        problems.append(f"X1 xdist scheduler API: {e}")
    if problems:
        raise pytest.UsageError("pytest-lanes refuses to run (fail-closed):\n  " + "\n  ".join(problems))


class LanesSession:
    def __init__(self, config, worker_mode: bool = False) -> None:
        self.worker_mode = worker_mode
        self.excl_lock = _RWLock()
        self.config = config
        self.n = config.getoption("lanes")
        self.events: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.uid = uuid.uuid4().hex
        self.sched = None

    # ---- install / uninstall -------------------------------------------------
    def _install(self, session) -> None:
        from _pytest import runner  # noqa: PLC0415
        from _pytest.fixtures import FixtureDef  # noqa: PLC0415

        _patch_fixturedef(FixtureDef)
        # P6: runner does os.environ.pop("PYTEST_CURRENT_TEST") with no default; two lanes
        # finishing together raise KeyError in teardown (seen 3/1000 under load).
        orig_update = runner._update_current_test_var

        def _update_current_test_var(item, when):
            if when:
                orig_update(item, when)
            else:
                os.environ.pop("PYTEST_CURRENT_TEST", None)

        self._orig_update = orig_update
        runner._update_current_test_var = _update_current_test_var
        self._main_ss = session._setupstate                      # P1
        self._ss_cls = type(self._main_ss)
        session._setupstate = _SetupStateRouter(self._main_ss)

        pm = self.config.pluginmanager                            # P4
        self._inner = inner = pm._inner_hookexec
        events = self.events

        def hookexec(name, impls, kwargs, firstresult):
            lane = _LANE.get()
            if lane is not None and name in ROUTED:
                if name == "pytest_runtest_logreport":
                    rep = kwargs["report"]
                    rep.lane_id = lane.gateway.id          # serializable; survives xdist transport
                    if not self.worker_mode:
                        rep.node = lane                    # in-process controller: xdist-style node
                events.put((name, impls, kwargs, firstresult))
                return None if firstresult else []
            return inner(name, impls, kwargs, firstresult)

        pm._inner_hookexec = hookexec

        self._log_templates = {}
        lp = pm.get_plugin("logging-plugin")                     # P3
        if lp is not None:
            self._lp = lp
            self._lp_orig = (lp.caplog_handler, lp.report_handler)
            self._log_templates = {"caplog": lp.caplog_handler, "report": lp.report_handler}
            lp.caplog_handler = _LogDispatch("caplog", lp.caplog_handler)
            lp.report_handler = _LogDispatch("report", lp.report_handler)
            root = logging.getLogger()
            if lp.log_level is not None:  # pre-lower so catching_logs restores are no-ops
                root.setLevel(min(root.level, lp.log_level))
            self._router = _LogRouter()
            self._routed_loggers = [root] + [
                lg for lg in root.manager.loggerDict.values()
                if isinstance(lg, logging.Logger) and not lg.propagate
            ]
            for lg in self._routed_loggers:
                lg.addHandler(self._router)

        self._real_out, self._real_err = sys.stdout, sys.stderr
        sys.stdout = _LaneStream(sys.stdout, "out")
        sys.stderr = _LaneStream(sys.stderr, "err")

    def _uninstall(self, session) -> None:
        from _pytest import runner  # noqa: PLC0415

        runner._update_current_test_var = self._orig_update
        sys.stdout, sys.stderr = self._real_out, self._real_err
        self.config.pluginmanager._inner_hookexec = self._inner
        session._setupstate = self._main_ss
        if hasattr(self, "_lp"):
            self._lp.caplog_handler, self._lp.report_handler = self._lp_orig
            for lg in self._routed_loggers:
                lg.removeHandler(self._router)

    # ---- per-phase capture -----------------------------------------------------
    def _phase(self, item, when):
        lane = _LANE.get()
        if lane is None:
            return (yield)
        lane.out, lane.err = io.StringIO(), io.StringIO()
        try:
            return (yield)
        finally:
            item.add_report_section(when, "stdout", lane.out.getvalue())
            item.add_report_section(when, "stderr", lane.err.getvalue())

    @pytest.hookimpl(wrapper=True, trylast=True)  # same nesting as CaptureManager
    def pytest_runtest_setup(self, item):
        return (yield from self._phase(item, "setup"))

    @pytest.hookimpl(wrapper=True, trylast=True)  # same nesting as CaptureManager
    def pytest_runtest_call(self, item):
        return (yield from self._phase(item, "call"))

    @pytest.hookimpl(wrapper=True, trylast=True)  # same nesting as CaptureManager
    def pytest_runtest_teardown(self, item):
        return (yield from self._phase(item, "teardown"))

    # ---- scheduler (same factory hook xdist uses) --------------------------------
    def _make_scheduler(self):
        from xdist.remote import Producer  # noqa: PLC0415
        from xdist.scheduler import (  # noqa: PLC0415
            LoadFileScheduling, LoadGroupScheduling, LoadScheduling, LoadScopeScheduling,
        )

        opt = self.config.option
        # xdist schedulers size themselves from --tx; present N lanes as N nodes, then restore.
        saved = getattr(opt, "tx", None)
        opt.tx = [f"{self.n}*popen"]
        try:
            log = Producer("lanessched", enabled=False)
            sched = self.config.hook.pytest_xdist_make_scheduler(config=self.config, log=log)
            if sched is None:
                cls = {"load": LoadScheduling, "loadscope": LoadScopeScheduling,
                       "loadfile": LoadFileScheduling, "loadgroup": LoadGroupScheduling,
                       }[self.config.getoption("lanes_dist")]
                sched = cls(self.config, log)
        finally:
            opt.tx = saved
        # X1 probe: the 4 members every load* scheduler touches must exist on it.
        missing = [m for m in ("add_node", "add_node_collection", "schedule", "mark_test_complete",
                               "tests_finished", "collection_is_completed") if not hasattr(sched, m)]
        if missing:
            raise pytest.UsageError(f"pytest-lanes: scheduler {type(sched).__name__} lacks {missing}")
        sched.numnodes = self.n
        name = type(sched).__name__
        if name in ("EachScheduling", "WorkStealingScheduling"):
            raise pytest.UsageError(f"{name} is not supported by lanes")
        return sched

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        from xdist.scheduler import LoadGroupScheduling  # noqa: PLC0415

        if self.worker_mode:   # the controller schedules; xdist's worker already did the suffix
            return

        self.sched = self._make_scheduler()
        if isinstance(self.sched, LoadGroupScheduling):
            # Identical to xdist.remote.WorkerInteractor.pytest_collection_modifyitems,
            # so nodeids (and everything keyed on them) match an -n loadgroup run.
            for item in items:
                gnames = set()
                for mark in item.iter_markers("xdist_group"):
                    gnames.add(str(mark.args[0] if mark.args else mark.kwargs.get("name", "default")))
                if gnames:
                    item._nodeid = f"{item.nodeid}@{'_'.join(sorted(gnames))}"  # P5

    # ---- node loop: same algorithm as xdist.remote.WorkerInteractor ----------------
    def _node_loop(self, node: ThreadNode, items) -> None:
        token = _LANE.set(node)
        hook = self.config.hook
        try:
            nxt = node.queue.get()
            while nxt is not _SHUTDOWN:
                idx = nxt
                nxt = node.queue.get()          # lookahead decides nextitem (teardown scope)
                if self.stop.is_set():
                    break
                item = items[idx]
                nextitem = None if nxt is _SHUTDOWN else items[nxt]
                start = time.perf_counter()
                guard = self.excl_lock.exclusive if self._is_exclusive(item) else self.excl_lock.shared
                with guard():
                    hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
                # Controller consumes reports + mark_test_complete before we continue,
                # so -x/--maxfail stop exactly like a sequential run.
                ack = threading.Event()
                self.events.put(("__done__", (node, idx, time.perf_counter() - start, ack), None, None))
                ack.wait()
                if self.stop.is_set():
                    break
            node.setupstate.teardown_exact(None)
        finally:
            _LANE.reset(token)

    def _pump(self, threads, sched, session, nodes, on_done=None, before_replay=None) -> None:
        while True:
            try:
                name, a, b, c = self.events.get(timeout=0.05)
            except queue.Empty:
                if not any(t.is_alive() for t in threads) and self.events.empty():
                    break
                continue
            if name == "__done__":
                node, idx, duration, ack = a
                if on_done is not None:
                    on_done(node, idx, duration)
                if sched is not None:
                    sched.mark_test_complete(node, idx, duration)
                    if sched.tests_finished:
                        for n in nodes:
                            n.shutdown()
                if session.shouldfail or session.shouldstop:
                    self.stop.set()
                    for n in nodes:
                        n.shutdown()
                ack.set()
                continue
            if before_replay is not None:
                before_replay(name, b)
            self._inner(name, a, b, c)
        for t in threads:
            t.join()
        if self._errors:
            raise self._errors[0]

    def _start(self, node, items) -> threading.Thread:
        def run():
            try:
                self._node_loop(node, items)
            except BaseException as e:  # surfaced as INTERNALERROR on the main thread
                self._errors.append(e)
        t = threading.Thread(target=run, name=f"lane-{node.gateway.id}", daemon=True)
        t.start()
        return t

    def _is_exclusive(self, item) -> bool:
        excl = set(self.config.getini("lanes_exclusive_fixtures"))
        return bool(item.get_closest_marker("lanes_exclusive") or excl & set(getattr(item, "fixturenames", ())))

    def _worker_runtestloop(self, session):
        """Replaces xdist.remote.WorkerInteractor.pytest_runtestloop in hybrid mode."""
        self._errors = []
        pm = self.config.pluginmanager
        interactor = next(p for p in pm.get_plugins() if type(p).__name__ == "WorkerInteractor")  # X2
        wid = self.config.workerinput["workerid"]
        nodes = [ThreadNode(f"{wid}.ln{i}", self.n, self.uid, self._ss_cls, self._log_templates)
                 for i in range(self.n)]
        end = object()

        def on_command(cmd):   # runs on execnet's receiver thread; only touches thread-safe queues
            if cmd is end:
                for n in nodes:
                    n.shutdown()
                return
            name, kw = cmd
            if name == "lanes_runtests":
                nodes[kw["lane"]].send_runtest_some(kw["indices"])
            elif name == "lanes_shutdown":
                nodes[kw["lane"]].shutdown()
            elif name == "shutdown":
                for n in nodes:
                    n.shutdown()
            else:
                self._errors.append(RuntimeError(f"pytest-lanes: unsupported xdist command {name!r}"))
                for n in nodes:
                    n.shutdown()

        interactor.channel.setcallback(on_command, endmarker=end)
        threads = [self._start(n, session.items) for n in nodes]

        def done(node, idx, duration):     # same event xdist's worker sends after each item
            interactor.sendevent("runtest_protocol_complete", item_index=idx, duration=duration)

        index_of = {it.nodeid: i for i, it in enumerate(session.items)}

        def before_replay(name, kwargs):   # WorkerInteractor reads self.item_index in logreport (X2)
            if name == "pytest_runtest_logreport":
                interactor.item_index = index_of[kwargs["report"].nodeid]

        self._pump(threads, None, session, nodes, on_done=done, before_replay=before_replay)
        return True

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        if self.worker_mode:
            return self._worker_runtestloop(session)
        if session.testsfailed and not session.config.option.continue_on_collection_errors:
            raise session.Interrupted(f"{session.testsfailed} errors during collection")
        if session.config.option.collectonly:
            return True
        self._errors: list = []
        excl = set(self.config.getini("lanes_exclusive_fixtures"))
        parallel, serial = [], []
        for it in session.items:
            (serial if it.get_closest_marker("lanes_exclusive") or excl & set(getattr(it, "fixturenames", ()))
             else parallel).append(it)

        mk = lambda i: ThreadNode(i, self.n, self.uid, self._ss_cls, self._log_templates)  # noqa: E731
        nodes = [mk(f"ln{i}") for i in range(self.n)]
        hooks = self.config.hook
        fire = self.config.getoption("lanes_xdist_node_hooks") and hasattr(hooks, "pytest_testnodeready")
        if fire:
            pm = self.config.pluginmanager
            allow = self.config.getini("lanes_node_hook_plugins")
            others = [p for name, p in pm.list_name_plugin()
                      if p is not None and not any(tok in str(name) for tok in allow)]
            ready = pm.subset_hook_caller("pytest_testnodeready", remove_plugins=others)
            down = pm.subset_hook_caller("pytest_testnodedown", remove_plugins=others)
            for n in nodes:
                ready(node=n)
        try:
            if parallel:
                sched = self.sched
                ids = [it.nodeid for it in parallel]
                for n in nodes:                      # DSession.worker_workerready
                    sched.add_node(n)
                for n in nodes:                      # DSession.worker_collectionfinish
                    sched.add_node_collection(n, ids)
                threads = [self._start(n, parallel) for n in nodes]
                if sched.collection_is_completed:
                    sched.schedule()
                self._pump(threads, sched, session, nodes)
            if serial and not self.stop.is_set():
                sn = mk("ln-serial")
                sn.send_runtest_some(range(len(serial)))
                sn.shutdown()
                self._pump([self._start(sn, serial)], None, session, [sn])
        finally:
            if fire:
                for n in nodes:
                    down(node=n, error=None)
        if session.shouldfail:
            raise session.Failed(session.shouldfail)
        if session.shouldstop:
            raise session.Interrupted(session.shouldstop)
        return True

    @pytest.hookimpl(trylast=True)  # after runner.py creates session._setupstate
    def pytest_sessionstart(self, session):
        self._install(session)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session):
        self._uninstall(session)


class _RWLock:
    """Writer-preferring RW lock: exclusive tests wait for running tests, then run alone."""

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


# ============================================================ hybrid: controller side
class LaneProxy:
    """Virtual node = one lane inside one xdist worker process."""

    def __init__(self, wc, lane: int, mux) -> None:
        self.wc = wc
        self.lane = lane
        self.mux = mux
        self.gateway = SimpleNamespace(id=f"{wc.gateway.id}.ln{lane}", spec=wc.gateway.spec)
        self.shutting_down = False

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
    """Presents real xdist workers to DSession; presents N x M lanes to the real scheduler."""

    def __init__(self, inner, lanes_per_worker: int, config) -> None:
        self.inner = inner
        self.m = lanes_per_worker
        self.config = config
        self.vnodes: dict = {}
        self.owner: dict = {}
        inner.numnodes = inner.numnodes * lanes_per_worker

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
        vs = self.vnodes[wc] = [LaneProxy(wc, i, self) for i in range(self.m)]
        for v in vs:
            self.inner.add_node(v)

    def add_node_collection(self, wc, ids) -> None:
        for v in self.vnodes[wc]:
            self.inner.add_node_collection(v, ids)

    def schedule(self) -> None:
        self.inner.schedule()

    def mark_test_complete(self, wc, item_index, duration=0) -> None:
        self.inner.mark_test_complete(self.owner[item_index], item_index, duration)

    def remove_node(self, wc):
        vs = self.vnodes.pop(wc)
        live = set(self.inner.nodes)
        crashes = [c for c in (self.inner.remove_node(v) for v in vs if v in live) if c]
        if len(crashes) > 1:   # DSession reports one crash item per worker; report the rest ourselves
            ds = self.config.pluginmanager.get_plugin("dsession")
            for c in crashes[1:]:
                ds.handle_crashitem(c, wc)                                           # X3
        return crashes[0] if crashes else None

    def remove_pending_tests_from_node(self, *a, **k):
        raise NotImplementedError("--dist worksteal is not supported by lanes")

    def __getattr__(self, name):
        return getattr(self.inner, name)


class LanesController:
    def __init__(self, config) -> None:
        self.m = config.getoption("lanes")

    @pytest.hookimpl(wrapper=True)
    def pytest_xdist_make_scheduler(self, config, log):
        sched = yield                       # whatever xdist or your conftest returned
        name = type(sched).__name__
        if name in ("EachScheduling", "WorkStealingScheduling"):
            raise pytest.UsageError(f"{name} is not supported by lanes")
        return LaneMux(sched, self.m, config)

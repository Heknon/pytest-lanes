"""The shared-state detector (``--lanes-detect``): which tests change process-wide state.

A debugging mode, run sequentially without lanes. For each test it snapshots
process state (environment, cwd, sys.path, logging levels, signal handlers) and,
recursively, the state reachable from your own modules, classes and registered
plugin objects; it also records every mock.patch and monkeypatch target. The
report separates what is unsafe to run next to other tests (state that changes
per test) from what is fine (a cache set once, then stable).
"""
import json

import pytest
from lanes_testing import run

DETECT = ["--lanes-detect", "-p", "no:randomly"]


def detect(pytester, *args, timeout=60):
    path = pytester.path / "detect.json"
    r = run(pytester, *DETECT, f"--lanes-detect-report={path}", *args, timeout=timeout)
    report = json.loads(path.read_text()) if path.exists() else None
    return r, report


def findings(report, kind):
    return {f["path"]: f for f in report["findings"] if f["kind"] == kind}


# ---------------------------------------------------------------- the user's real pattern
ACTIVE_TEST_PLUGIN = """
import pytest

class Tracker:                     # nested plugin-like object, reached only through the plugin
    def __init__(self):
        self.last_nodeid = None

class ActiveTestPlugin:
    # Registered once at configure; assumes one test per process.
    def __init__(self):
        self.active_test = None
        self.children = [Tracker()]
        self.by_name = {"tracker": self.children[0]}
        self.me = self                 # cycles must not hang the walk

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self.active_test = item.nodeid
        self.children[0].last_nodeid = item.nodeid

    def pytest_runtest_logfinish(self, nodeid):
        self.active_test = None       # reset after: still unsafe while tests overlap

def pytest_configure(config):
    config.pluginmanager.register(ActiveTestPlugin(), "active-test-plugin")
"""


def test_plugin_holding_the_active_test_is_unsafe(pytester):
    pytester.makeconftest(ACTIVE_TEST_PLUGIN)
    pytester.makepyfile(test_a="def test_1(): pass\ndef test_2(): pass\ndef test_3(): pass\n")
    r, report = detect(pytester)
    r.assert_outcomes(passed=3)
    unsafe = findings(report, "per-test")
    assert "plugin:active-test-plugin.active_test" in unsafe, report
    # Found recursively: through a list, a dict and a nested object.
    assert "plugin:active-test-plugin.children[0].last_nodeid" in unsafe, report
    assert unsafe["plugin:active-test-plugin.active_test"]["tests"] == 3
    r.stdout.fnmatch_lines(["*shared-state report*", "*UNSAFE*plugin:active-test-plugin.active_test*"])


def test_class_attribute_and_module_global_changed_per_test_are_unsafe(pytester):
    pytester.makepyfile(infra="""
        class Manager:
            current = None
        STATE = {"env": None}
    """)
    pytester.makepyfile(test_a="""
        import pytest, infra
        @pytest.mark.parametrize("env", ["a", "b", "c"])
        def test_t(env):
            infra.Manager.current = env
            infra.STATE["env"] = env
    """)
    r, report = detect(pytester)
    r.assert_outcomes(passed=3)
    unsafe = findings(report, "per-test")
    assert "module:infra.Manager.current" in unsafe, report
    assert "module:infra.STATE['env']" in unsafe, report


def test_cache_set_once_is_fine(pytester):
    pytester.makepyfile(clients="""
        _CLIENTS = {}
        class SaharaClient:
            def __init__(self):
                self.base_url = "https://api"
        def get_client():
            if "sahara" not in _CLIENTS:
                _CLIENTS["sahara"] = SaharaClient()
            return _CLIENTS["sahara"]
    """)
    pytester.makepyfile(test_a="""
        import pytest, clients
        @pytest.mark.parametrize("i", range(4))
        def test_t(i):
            assert clients.get_client().base_url
    """)
    r, report = detect(pytester)
    r.assert_outcomes(passed=4)
    assert findings(report, "per-test") == {}, report
    # Reported once, at the cache: its entries (and their attributes) are left out.
    assert set(findings(report, "set-once")) == {"module:clients._CLIENTS"}, report


def test_ignored_paths_are_not_reported(pytester):
    pytester.makeini("""
        [pytest]
        lanes_detect_ignore = module:infra.COUNTER
    """)
    pytester.makepyfile(infra="COUNTER = 0\nOTHER = 0\n")
    pytester.makepyfile(test_a="""
        import pytest, infra
        @pytest.mark.parametrize("i", range(3))
        def test_t(i):
            infra.COUNTER += 1
            infra.OTHER += 1
    """)
    _, report = detect(pytester)
    unsafe = findings(report, "per-test")
    assert "module:infra.COUNTER" not in unsafe and "module:infra.OTHER" in unsafe, report


def test_growing_container_is_reported_separately(pytester):
    pytester.makepyfile(infra="RESULTS = []\n")
    pytester.makepyfile(test_a="""
        import pytest, infra
        @pytest.mark.parametrize("i", range(4))
        def test_t(i):
            infra.RESULTS.append(i)
    """)
    _, report = detect(pytester)
    assert "module:infra.RESULTS" in findings(report, "grows"), report
    assert not any(p.startswith("module:infra.RESULTS[") for p in findings(report, "per-test")), report


# ---------------------------------------------------------------- patches and process state
def test_patches_inside_a_test_body_are_recorded(pytester):
    # Undone before the test ends, so no snapshot sees them: they are recorded as they happen.
    pytester.makepyfile(test_a="""
        import json, os
        from unittest import mock
        def test_mock():
            with mock.patch("json.dumps", return_value="x"):
                assert json.dumps(1) == "x"
        def test_mock_dict():
            with mock.patch.dict(os.environ, {"LANES_T": "1"}):
                pass
        def test_monkeypatch(monkeypatch):
            monkeypatch.setattr(json, "loads", lambda s: 0)
        def test_environ_restored_in_body():
            os.environ["LANES_E"] = "1"
            del os.environ["LANES_E"]
        def test_chdir_restored_in_body(tmp_path):
            old = os.getcwd(); os.chdir(tmp_path); os.chdir(old)
        def test_clean():
            pass
    """)
    r, report = detect(pytester)
    r.assert_outcomes(passed=6)
    patched = findings(report, "patched")
    assert patched["json.dumps"]["examples"] == ["test_a.py::test_mock"], report
    assert "json.loads" in patched and "env:LANES_T" in patched, report
    assert "env:LANES_E" in patched and "cwd" in patched, report
    assert not any("test_clean" in f["examples"] for f in report["findings"]), report


def test_process_state_changed_by_fixtures_is_unsafe(pytester):
    pytester.makepyfile(test_a="""
        import logging, os, sys, pytest
        @pytest.mark.parametrize("i", range(2))
        def test_level(i, caplog):
            caplog.set_level(logging.DEBUG, logger="infra")
        @pytest.mark.parametrize("i", range(2))
        def test_path(i, monkeypatch):
            monkeypatch.syspath_prepend("/nonexistent")
    """)
    _, report = detect(pytester)
    unsafe = {**findings(report, "per-test"), **findings(report, "patched")}
    assert "logging:infra.level" in unsafe and "sys.path" in unsafe, report
    # Every test that changed shared state, for marking lanes_exclusive.
    assert report["unsafe_tests"] == sorted(
        [f"test_a.py::test_level[{i}]" for i in range(2)] + [f"test_a.py::test_path[{i}]" for i in range(2)])


def test_clean_suite_reports_nothing_unsafe(pytester):
    pytester.makepyfile(test_a="""
        import pytest
        @pytest.fixture(scope="session")
        def sess():
            return object()
        @pytest.mark.parametrize("i", range(5))
        def test_t(i, sess, tmp_path):
            (tmp_path / "f").write_text("x")
            print("out")
    """)
    r, report = detect(pytester)
    r.assert_outcomes(passed=5)
    assert [f for f in report["findings"] if f["kind"] in ("per-test", "patched", "grows")] == [], report
    r.stdout.fnmatch_lines(["*shared-state report*", "*5 tests*nothing unsafe found*"])


# ---------------------------------------------------------------- walk safety and usage
def test_walk_never_calls_properties_and_survives_odd_objects(pytester):
    pytester.makepyfile(infra="""
        class Boom:
            @property
            def value(self):
                raise RuntimeError("a property was evaluated")
            def __getattr__(self, name):
                raise RuntimeError("__getattr__ was called")
            def __eq__(self, other):
                raise RuntimeError("__eq__ was called")
            __hash__ = object.__hash__
        class Slotted:
            __slots__ = ("x",)
            def __init__(self): self.x = 0
        def deep(n):
            return {"d": deep(n - 1)} if n else 0
        BOOM = Boom()
        SLOTTED = Slotted()
        DEEP = deep(500)
        CYCLE = []; CYCLE.append(CYCLE)
    """)
    pytester.makepyfile(test_a="""
        import pytest, infra
        @pytest.mark.parametrize("i", range(2))
        def test_t(i):
            infra.SLOTTED.x = i + 1
    """)
    r, report = detect(pytester)
    r.assert_outcomes(passed=2)
    assert "module:infra.SLOTTED.x" in findings(report, "per-test"), report


@pytest.mark.parametrize("mode", [["--lanes", "2"], ["-n", "2"]], ids=["lanes", "xdist"])
def test_detect_refuses_to_run_concurrently(pytester, mode):
    pytester.makepyfile(test_a="def test_t(): pass\n")
    r = run(pytester, *DETECT, *mode)
    assert r.ret == pytest.ExitCode.USAGE_ERROR
    r.stderr.fnmatch_lines(["*--lanes-detect runs tests one at a time*"])


# ---------------------------------------------------------------- keeping the report readable
def test_clearing_the_environment_is_one_finding(pytester, monkeypatch):
    for i in range(30):
        monkeypatch.setenv(f"LANES_BULK_{i}", "x")
    pytester.makepyfile(test_a="""
        import os
        from unittest import mock
        def test_clear():
            with mock.patch.dict(os.environ, clear=True):
                pass
    """)
    _, report = detect(pytester)
    patched = findings(report, "patched")
    assert "env:*" in patched, report
    assert not any(p.startswith("env:LANES_BULK_") for p in patched), report


def test_a_patched_global_is_reported_once(pytester):
    pytester.makepyfile(infra="def connect(): return 'real'\n")
    pytester.makepyfile(test_a="""
        import pytest, infra
        @pytest.mark.parametrize("i", range(2))
        def test_t(i, monkeypatch):
            monkeypatch.setattr(infra, "connect", lambda: "fake")
            monkeypatch.setenv("LANES_X", str(i))
    """)
    _, report = detect(pytester)
    patched, per_test = findings(report, "patched"), findings(report, "per-test")
    assert "infra.connect" in patched and "env:LANES_X" in patched, report
    assert "module:infra.connect" not in per_test and "env:LANES_X" not in per_test, report


def test_ok_findings_are_counted_not_listed(pytester):
    pytester.makepyfile(infra="CACHE = {}\n")
    pytester.makepyfile(test_a="""
        import infra
        def test_t():
            infra.CACHE["k"] = 1
    """)
    r, report = detect(pytester)
    assert "module:infra.CACHE" in findings(report, "set-once"), report
    out = r.stdout.str()
    assert "module:infra.CACHE" not in out, out
    r.stdout.fnmatch_lines(["*1 set-once (caches set once, then stable)*JSON*"])


# ---------------------------------------------------------------- process-wide setters and stdio (round 4)
def test_process_wide_setters_and_stdio_swaps_are_recorded(pytester):
    # Each broke concurrent tests in round 4 while the detector saw nothing.
    pytester.makepyfile(test_a="""
        import contextlib, gc, io, random, socket, sys
        def test_seed():
            random.seed(42)
        def test_socket_timeout():
            old = socket.getdefaulttimeout(); socket.setdefaulttimeout(5); socket.setdefaulttimeout(old)
        def test_recursion_limit():
            old = sys.getrecursionlimit(); sys.setrecursionlimit(old + 1); sys.setrecursionlimit(old)
        def test_gc():
            gc.disable(); gc.enable()
        def test_swap_stdout():
            real = sys.stdout; sys.stdout = io.StringIO()
            try:
                import time; time.sleep(0.05)
            finally:
                sys.stdout = real
        def test_redirect_is_per_lane():
            with contextlib.redirect_stdout(io.StringIO()):
                import time; time.sleep(0.05)
        def test_private_rng_is_fine():
            random.Random(1).random()
    """)
    r, report = detect(pytester)
    r.assert_outcomes(passed=7)
    patched = findings(report, "patched")
    for path, test in [("random.seed()", "test_seed"), ("socket.setdefaulttimeout()", "test_socket_timeout"),
                       ("sys.setrecursionlimit()", "test_recursion_limit"), ("gc.disable()", "test_gc"),
                       ("sys.stdout", "test_swap_stdout")]:
        assert path in patched and patched[path]["examples"] == [f"test_a.py::{test}"], (path, report)
    unsafe_tests = set(report["unsafe_tests"])
    assert "test_a.py::test_redirect_is_per_lane" not in unsafe_tests, report
    assert "test_a.py::test_private_rng_is_fine" not in unsafe_tests, report


# ---------------------------------------------------------------- round 6 review
def test_hostile_globals_neither_crash_nor_fail_tests(pytester):
    # isinstance() reads __class__: a dead weakref.proxy or a werkzeug-like LocalProxy
    # raised INTERNALERROR, and a spec'd mock passed isinstance(x, dict) and then broke
    # dict.items(x), failing a passing test.
    pytester.makepyfile(infra="""
        import weakref
        class Thing: pass
        _t = Thing()
        DEAD = weakref.proxy(_t)
        del _t
        class LocalProxy:
            @property
            def __class__(self):
                raise RuntimeError("Working outside of application context")
        request = LocalProxy()
        REGISTRY = {"a": 1}
        NAMES = ["a"]
        TITLE = "t"
    """, test_a="""
        import infra
        def test_spec_dict(mocker):
            mocker.patch.object(infra, "REGISTRY", spec=dict)
            mocker.patch.object(infra, "NAMES", spec=list)
            mocker.patch.object(infra, "TITLE", spec=str)
        def test_other():
            pass
    """)
    r, report = detect(pytester)
    r.assert_outcomes(passed=2)
    assert "INTERNALERROR" not in r.stdout.str()


def test_set_changed_by_a_background_thread(pytester):
    pytester.makepyfile(infra="""
        import sys, threading, time
        sys.setswitchinterval(1e-5)
        ACTIVE = set(range(5000))
        def _churn():
            i = 5000
            while True:
                for _ in range(500):
                    ACTIVE.add(i); ACTIVE.discard(i - 5000); i += 1
                time.sleep(0.0001)
        threading.Thread(target=_churn, daemon=True).start()
    """, test_a="""
        import infra, pytest
        @pytest.mark.parametrize("i", range(100))
        def test_x(i): pass
    """)
    r, report = detect(pytester, timeout=120)
    r.assert_outcomes(passed=100)
    assert "INTERNALERROR" not in r.stdout.str()


def test_node_cap_does_not_invent_findings(pytester):
    pytester.makeini("[pytest]\nlanes_detect_max_nodes = 400\n")
    pytester.makepyfile(aaa="LOG = []\n", zzz="\n".join(f"V{i} = {i}" for i in range(400)),
                        test_a="""
        import aaa, zzz, pytest
        @pytest.fixture
        def buf():
            aaa.LOG.extend(range(20))
            yield
            aaa.LOG.clear()
        @pytest.mark.parametrize("i", range(3))
        def test_x(i, buf):
            pass
    """)
    r, report = detect(pytester)
    assert report["truncated"]
    assert not [f for f in report["findings"] if "zzz" in f["path"]], report["findings"]


def test_object_reached_by_two_paths_is_not_reported_as_changed(pytester):
    pytester.makepyfile(aaa="current = None\n", zzz="""
        class Env:
            def __init__(self, name): self.name = name; self.hosts = ["h1", "h2"]
        ENVS = [Env("e1"), Env("e2")]
    """, test_a="""
        import pytest, aaa, zzz
        @pytest.fixture
        def env():
            aaa.current = zzz.ENVS[0]
            yield
            aaa.current = None
        def test_x(env): pass
    """)
    r, report = detect(pytester)
    assert set(findings(report, "per-test")) == {"module:aaa.current"}, report["findings"]


def test_patches_of_test_local_objects_are_not_findings(pytester):
    # The run-time guard (P14) allows them; the detector reported each as UNSAFE.
    pytester.makepyfile(test_a="""
        from unittest import mock
        class Client:
            def send(self): return 1
        def test_local_instance(mocker, monkeypatch):
            c = Client()
            mocker.patch.object(c, "send", return_value=2)
            monkeypatch.setattr(c, "timeout", 5, raising=False)
            d = {}
            monkeypatch.setitem(d, "k", 1)
            with mock.patch.dict(d, {"x": 1}):
                pass
            assert c.send() == 2
        def test_cls_attr(mocker):
            mocker.patch.object(Client, "send")
    """)
    r, report = detect(pytester)
    r.assert_outcomes(passed=2)
    unsafe = [f for f in report["findings"] if f["severity"] == "unsafe"]
    # The class patch, once, by its module-qualified name (so lanes_detect_ignore can match it).
    assert [(f["kind"], f["path"], f["nodeids"]) for f in unsafe] == [
        ("patched", "test_a.Client.send", ["test_a.py::test_cls_attr"])], unsafe


def test_wider_scoped_fixture_is_named_not_the_first_and_last_test(pytester):
    pytester.makepyfile(infra='MODE = "off"\n')
    pytester.makeconftest("""
        import pytest, infra
        @pytest.fixture(scope="session", autouse=True)
        def mode():
            infra.MODE = "on"
            yield
            infra.MODE = "off"
    """)
    pytester.makepyfile(test_a="""
        import infra
        def test_1(): assert infra.MODE == "on"
        def test_2(): assert infra.MODE == "on"
        def test_3(): assert infra.MODE == "on"
    """)
    r, report = detect(pytester)
    r.assert_outcomes(passed=3)
    f = findings(report, "per-test")["module:infra.MODE"]
    assert f["nodeids"] == ["fixture mode (session scope)"], f
    assert report["unsafe_tests"] == [], report["unsafe_tests"]
    assert "fixture mode (session scope)" in report["unsafe_fixtures"]


def test_short_stdio_swap_is_recorded(pytester):
    # The 1 ms poller missed a swap shorter than a GIL switch interval.
    pytester.makepyfile(test_a="""
        import io, os, sys
        def test_swap():
            old = sys.stdout
            sys.stdout = io.StringIO()
            open(os.devnull).close()       # any audited operation while it is swapped
            sys.stdout = old
    """)
    r, report = detect(pytester)
    assert "sys.stdout" in findings(report, "patched"), report["findings"]

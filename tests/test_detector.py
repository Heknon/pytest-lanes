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

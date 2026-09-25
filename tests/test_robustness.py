"""Robustness: failure paths, odd options and odd run shapes must end cleanly, like xdist.

"Cleanly" means: no hang (every run here has a timeout), no INTERNALERROR that
xdist would not also raise, and the same exit code as plain xdist.
"""
import inspect
import sys

import _pytest.runner
import pytest
from lanes_testing import BASE, MODES, run

#: pytest >= 8.1 tears a test down fully once the session is stopping (-x). Older
#: runners leave session fixtures to the end of the session, even without xdist.
RUNNER_TEARS_DOWN_ON_STOP = "shouldfail" in inspect.getsource(_pytest.runner.runtestprotocol)

ENV_SCHEDULER = """
from xdist.scheduler import LoadScopeScheduling
class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid):
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]
def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)
"""
ENV_TESTS = """
import pytest, time
@pytest.mark.parametrize("step", range(3))
@pytest.mark.parametrize("env", [f"env{c}" for c in "ABCDEF"])
def test_s(env, step):
    time.sleep(0.02)
    {body}
"""


# ---------------------------------------------------------------- a lane dies
@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_plugin_error_inside_protocol_ends_run_without_hanging(pytester, mode):
    # An exception escaping pytest_runtest_protocol kills that lane's thread (or xdist's
    # worker). Its remaining scope can never finish; the run must still end.
    pytester.makeconftest(ENV_SCHEDULER + """
import pytest
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if item.name == "test_s[envB-1]":
        raise RuntimeError("plugin bug")
    return (yield)
""")
    pytester.makepyfile(ENV_TESTS.replace("{body}", "pass"))
    r = run(pytester, *mode, timeout=60)
    assert r.ret == pytest.ExitCode.INTERNAL_ERROR
    r.stdout.fnmatch_lines(["INTERNALERROR*RuntimeError: plugin bug"])


@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_keyboard_interrupt_in_a_test_ends_run_interrupted(pytester, mode):
    pytester.makeconftest(ENV_SCHEDULER)
    body = 'if env == "envB" and step == 1: raise KeyboardInterrupt'
    pytester.makepyfile(ENV_TESTS.replace("{body}", body))
    r = run(pytester, *mode, timeout=60)
    assert r.ret == pytest.ExitCode.INTERRUPTED


@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_pytest_exit_in_a_test_ends_run(pytester, mode):
    pytester.makepyfile(ENV_TESTS.replace("{body}", 'if env == "envB": pytest.exit("bye", returncode=7)'))
    r = run(pytester, *mode, timeout=60)
    assert r.ret != 0


@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_reporter_error_during_replay_ends_run_as_internal_error(pytester, mode):
    pytester.makeconftest("""
        def pytest_runtest_logreport(report):
            if report.when == "call" and report.nodeid.endswith("[envB-1]"):
                raise RuntimeError("reporter bug")
    """)
    pytester.makepyfile(ENV_TESTS.replace("{body}", "pass"))
    r = run(pytester, *mode, timeout=60)
    assert r.ret == pytest.ExitCode.INTERNAL_ERROR


# ---------------------------------------------------------------- -x / --maxfail
SESSION_TEARDOWN_FAILS = """
import pytest
@pytest.fixture(scope="session")
def s():
    yield
    raise RuntimeError("teardown boom")
"""


@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_exitfirst_reports_session_teardown_error_as_test_error(pytester, mode):
    # pytest tears the failing test down with nextitem=None once session.shouldfail is
    # set, so session-fixture teardown errors become test ERRORs, never INTERNALERROR.
    pytester.makeconftest(SESSION_TEARDOWN_FAILS)
    pytester.makepyfile("""
        import pytest, time
        @pytest.mark.parametrize("i", range(8))
        def test_t(i, s):
            time.sleep(0.05)
            assert i != 2
    """)
    r = run(pytester, *mode, "-x", timeout=60)
    assert "INTERNALERROR" not in r.stdout.str()
    if RUNNER_TEARS_DOWN_ON_STOP:
        r.stdout.fnmatch_lines(["*ERROR at teardown of test_t*", "*RuntimeError: teardown boom*"])
    elif mode[0] == "--lanes":   # left for the end of the lane: still reported, not raised
        r.stdout.fnmatch_lines(["*ERROR tearing down lane ln* after the run stopped: "
                                "RuntimeError: teardown boom*"])
    assert r.ret == pytest.ExitCode.INTERRUPTED                    # as xdist


@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_exitfirst_exit_code_matches_xdist(pytester, mode):
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("i", range(6))
        def test_t(i): assert i != 1
    """)
    r = run(pytester, *mode, "-x", timeout=60)
    assert r.ret == pytest.ExitCode.INTERRUPTED                    # xdist raises Interrupted


# ---------------------------------------------------------------- run shapes
@pytest.mark.parametrize("mode", [["--lanes", "50"], ["-n", "2", "--lanes", "50"], ["--lanes", "1"]],
                         ids=["more-lanes-than-tests", "hybrid-more-lanes", "one-lane"])
def test_lane_count_extremes(pytester, mode):
    pytester.makepyfile("import pytest\n@pytest.mark.parametrize('i', range(3))\ndef test_t(i): pass")
    run(pytester, *mode, timeout=60).assert_outcomes(passed=3)


@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_nothing_to_run(pytester, mode):
    pytester.makepyfile("def test_t(): pass")
    assert run(pytester, *mode, "-k", "nomatch", timeout=60).ret == pytest.ExitCode.NO_TESTS_COLLECTED
    empty = pytester.mkdir("empty")
    assert run(pytester, *mode, str(empty), timeout=60).ret == pytest.ExitCode.NO_TESTS_COLLECTED


@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_only_exclusive_tests(pytester, mode):
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("i", range(3))
        def test_c(i, capsys):
            print(i); assert capsys.readouterr().out == f"{i}\\n"
    """)
    run(pytester, *mode, timeout=60).assert_outcomes(passed=3)


@pytest.mark.parametrize("flag", ["--co", "--setup-plan", "--setup-only", "--setup-show"])
def test_collect_and_setup_options(pytester, flag):
    pytester.makepyfile("import pytest\n@pytest.fixture\ndef f(): yield\n"
                        "@pytest.mark.parametrize('i', range(3))\ndef test_t(i, f): pass")
    r = run(pytester, "--lanes", "2", flag, timeout=60)
    assert r.ret == pytest.ExitCode.OK


@pytest.mark.parametrize("mode", MODES.values(), ids=MODES.keys())
def test_collection_error_runs_the_rest_like_xdist(pytester, mode):
    pytester.makepyfile(test_ok="def test_ok(): pass", test_bad="import no_such_module")
    r = run(pytester, *mode, timeout=60)
    r.assert_outcomes(passed=1, errors=1)
    assert r.ret == pytest.ExitCode.TESTS_FAILED


# ---------------------------------------------------------------- options
@pytest.mark.parametrize("mode", [["--lanes", "2"], ["-n", "2", "--lanes", "2"]], ids=["lanes", "hybrid"])
def test_trace_is_rejected_or_ignored_never_hangs(pytester, mode):
    # --trace opens pdb in every test; on a lane thread that blocks forever.
    pytester.makepyfile("def test_t(): pass")
    r = run(pytester, *mode, "--trace", timeout=60)
    if mode[0] == "--lanes":
        assert r.ret == pytest.ExitCode.USAGE_ERROR
        r.stderr.fnmatch_lines(["*--trace*"])


def test_negative_lane_count_is_a_usage_error(pytester):
    pytester.makepyfile("def test_t(): pass")
    r = run(pytester, "--lanes", "-1", timeout=60)
    assert r.ret == pytest.ExitCode.USAGE_ERROR
    r.stderr.fnmatch_lines(["*--lanes*"])


def test_zero_lanes_means_no_lanes(pytester):
    pytester.makepyfile("def test_t(): pass")
    run(pytester, "--lanes", "0", timeout=60).assert_outcomes(passed=1)


def test_dist_option_is_honoured_in_single_process_mode(pytester):
    # --dist loadgroup --lanes N must mean loadgroup, as it does with -n.
    pytester.makepyfile("""
        import pytest
        @pytest.mark.xdist_group(name="g")
        def test_t(): pass
    """)
    r = run(pytester, "--lanes", "2", "--dist", "loadgroup", "-v", timeout=60)
    r.stdout.fnmatch_lines(["*PASSED*::test_t@g*"])


@pytest.mark.parametrize("dist", ["each", "worksteal"])
@pytest.mark.parametrize("mode", [["--lanes", "2"], ["-n", "2", "--lanes", "2"]], ids=["lanes", "hybrid"])
def test_unsupported_dist_modes_are_rejected(pytester, mode, dist):
    pytester.makepyfile("def test_t(): pass")
    r = run(pytester, *mode, "--dist", dist, timeout=60)
    assert r.ret == pytest.ExitCode.USAGE_ERROR


@pytest.mark.parametrize("mode", [["--tx", "2*popen", "--lanes", "2"], ["-n", "auto", "--lanes", "2"],
                                  ["-n", "0", "--lanes", "2"]], ids=["tx", "n-auto", "n-zero"])
def test_other_ways_to_ask_for_processes(pytester, mode):
    pytester.makepyfile("import pytest\n@pytest.mark.parametrize('i', range(4))\ndef test_t(i): pass")
    run(pytester, *mode, timeout=60).assert_outcomes(passed=4)


# ---------------------------------------------------------------- hybrid failure paths
@pytest.mark.parametrize("restarts", [[], ["--max-worker-restart", "0"]], ids=["default", "no-restart"])
def test_hybrid_always_crashing_test_terminates(pytester, restarts):
    pytester.makepyfile("""
        import os, time, pytest
        @pytest.mark.parametrize("i", range(6))
        def test_t(i):
            time.sleep(0.05)
            if i == 2: os._exit(1)
    """)
    r = run(pytester, "-n", "2", "--lanes", "2", *restarts, timeout=90)
    assert r.ret == pytest.ExitCode.TESTS_FAILED


def test_hybrid_lane_id_reaches_controller_hooks(pytester):
    pytester.makeconftest("""
        IDS = set()
        def pytest_runtest_logreport(report):
            if report.when == "call": IDS.add(getattr(report, "lane_id", None))
        def pytest_sessionfinish(session):
            if not hasattr(session.config, "workerinput"):
                print("\\nLANEIDS", sorted(map(str, IDS)))
    """)
    pytester.makepyfile("""
        import pytest, time
        @pytest.mark.parametrize("i", range(8))
        def test_t(i): time.sleep(0.1)
    """)
    r = run(pytester, "-n", "2", "--lanes", "2", "-s", timeout=60)
    r.stdout.fnmatch_lines(["LANEIDS ['gw0.ln0', 'gw0.ln1', 'gw1.ln0', 'gw1.ln1']"])


# ---------------------------------------------------------------- timeouts (fail closed)
@pytest.mark.parametrize("how", ["option", "ini", "env", "marker"])
def test_pytest_timeout_is_refused_in_single_process_mode(pytester, monkeypatch, how):
    # pytest-timeout cannot use signals on a lane thread; its thread method os._exit()s
    # the whole process, ending every lane with no reports. Refuse, pointing to hybrid.
    pytest.importorskip("pytest_timeout")
    args = []
    if how == "marker":
        pytester.makepyfile("import pytest\n@pytest.mark.timeout(5)\ndef test_t(): pass")
    else:
        pytester.makepyfile("def test_t(): pass")
        if how == "option":
            args = ["--timeout", "5"]
        elif how == "ini":
            pytester.makeini("[pytest]\ntimeout = 5\n")
        else:
            monkeypatch.setenv("PYTEST_TIMEOUT", "5")
    r = run(pytester, "--lanes", "2", *args, timeout=60)
    assert r.ret == pytest.ExitCode.USAGE_ERROR
    r.stderr.fnmatch_lines(["*pytest-timeout*"])


def test_pytest_timeout_off_or_in_hybrid_mode_is_fine(pytester):
    pytest.importorskip("pytest_timeout")
    pytester.makepyfile("def test_t(): pass")
    run(pytester, "--lanes", "2", "--timeout", "0", timeout=60).assert_outcomes(passed=1)
    run(pytester, "-n", "2", "--lanes", "2", "--timeout", "5", timeout=60).assert_outcomes(passed=1)


@pytest.mark.parametrize("mode", [["--lanes", "2"], ["-n", "2", "--lanes", "2"]], ids=["lanes", "hybrid"])
def test_faulthandler_timeout_is_refused(pytester, mode):
    # Its timer is process-wide and every test restarts or cancels it, so under lanes
    # it never fires for the test that hangs. Refuse rather than pretend.
    pytester.makepyfile("def test_t(): pass")
    pytester.makeini("[pytest]\nfaulthandler_timeout = 5\n")
    r = run(pytester, *mode, timeout=60)
    assert r.ret == pytest.ExitCode.USAGE_ERROR
    r.stderr.fnmatch_lines(["*faulthandler_timeout*"])


# ---------------------------------------------------------------- hybrid: interpreter flags (round 4)
WARNINGS_ON = [a for pair in zip(BASE[::2], BASE[1::2]) if pair != ("-p", "no:warnings") for a in pair]


@pytest.mark.skipif(bool(getattr(sys.flags, "context_aware_warnings", False)),
                    reason="warnings are context-aware here, so nothing is refused")
def test_hybrid_refuses_up_front_when_its_workers_would(pytester, monkeypatch):
    # Every worker refused (warnings plugin on, not context-aware), each with a traceback,
    # and the run ended "no tests ran" (exit 5). The controller now refuses first (exit 4).
    monkeypatch.delenv("PYTHON_CONTEXT_AWARE_WARNINGS", raising=False)
    pytester.makepyfile("def test_t(): pass\n")
    r = pytester.runpytest_subprocess(*WARNINGS_ON, "-n", "2", "--lanes", "2", timeout=60)
    assert r.ret == pytest.ExitCode.USAGE_ERROR, r.stdout.str() + r.stderr.str()
    r.stderr.fnmatch_lines(["*pytest-threadlanes refuses to run*", "*warnings plugin active*"])
    assert "Traceback" not in r.stdout.str() + r.stderr.str()


@pytest.mark.skipif(sys.version_info < (3, 14), reason="context-aware warnings need Python 3.14")
def test_context_aware_warnings_flag_reaches_hybrid_workers(pytester, monkeypatch):
    # -X context_aware_warnings=1 applies to the controller only: xdist starts its workers
    # without it, so every worker refused. The controller passes it on through the environment.
    monkeypatch.delenv("PYTHON_CONTEXT_AWARE_WARNINGS", raising=False)
    pytester.makepyfile("""
        import warnings
        def test_w():
            warnings.warn("w", UserWarning)
    """)
    r = pytester.run(sys.executable, "-X", "context_aware_warnings=1", "-m", "pytest", *WARNINGS_ON,
                     "-n", "1", "--lanes", "2", timeout=60)
    assert r.ret == 0, r.stdout.str() + r.stderr.str()
    r.stdout.fnmatch_lines(["*1 passed, 1 warning*"])


# ---------------------------------------------------------------- Ctrl-C (round 4)
INTERRUPTED_TESTS = """
import os, pathlib, time, pytest
OUT = pathlib.Path(os.environ["LANES_OUT"])
@pytest.fixture
def env(request):
    yield
    (OUT / f"teardown-{{request.node.name}}").write_text("released")
@pytest.fixture(scope="session")
def session_env(worker_id):
    yield
    (OUT / f"session-teardown-{{worker_id}}").write_text("released")
@pytest.mark.parametrize("i", range(4))
def test_long(i, env, session_env):
    (OUT / f"started-{{i}}").write_text("")
    {body}
"""


def interrupt(pytester, mode, body, *, ini="", wait_started=4, timeout=60):
    """Start a run, press Ctrl-C (SIGINT to the process group) once the tests run."""
    import os
    import signal
    import subprocess
    import time
    pytester.makepyfile(INTERRUPTED_TESTS.format(body=body))
    if ini:
        pytester.makeini(ini)
    env = {**os.environ, "LANES_OUT": str(pytester.path)}
    p = subprocess.Popen([sys.executable, "-m", "pytest", *BASE, *mode], cwd=pytester.path, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    deadline = time.monotonic() + 30
    while len(list(pytester.path.glob("started-*"))) < wait_started and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    started = time.monotonic()
    os.killpg(p.pid, signal.SIGINT)
    out, _ = p.communicate(timeout=timeout)
    return p.returncode, out, time.monotonic() - started


@pytest.mark.parametrize("mode", [["--lanes", "4"], ["-n", "2", "--lanes", "2"]], ids=["lanes", "hybrid"])
def test_ctrl_c_tears_down_the_running_tests(pytester, mode):
    # Plain pytest and xdist tear down the interrupted tests' fixtures (that is where
    # environments are released). Lanes abandoned their threads: no teardown ran at all.
    code, out, _ = interrupt(pytester, mode, "for _ in range(600): time.sleep(0.05)")
    assert code == pytest.ExitCode.INTERRUPTED, out
    assert len(list(pytester.path.glob("teardown-*"))) == 4, out
    assert len(list(pytester.path.glob("session-teardown-*"))) == 4, out


def test_ctrl_c_abandons_a_lane_blocked_past_the_grace_period(pytester):
    # A lane blocked in one long C call cannot be interrupted; after the grace period
    # the run ends anyway, and says which tests were left without teardown.
    code, out, took = interrupt(pytester, ["--lanes", "4"], "time.sleep(120 if i == 0 else 0.01)",
                                ini="[pytest]\nlanes_interrupt_grace = 2\n", wait_started=4)
    assert code == pytest.ExitCode.INTERRUPTED, out
    assert took < 30, took
    assert "test_long[0]" in out and "without teardown" in out, out



def test_ctrl_c_while_the_main_thread_handles_a_finished_test(pytester):
    # Ctrl-C while the main thread handled a lane's "item done" left that lane waiting
    # for an acknowledgement forever: the run waited out the grace period, and the
    # lane's fixtures were never torn down (round-5 review).
    import os
    import subprocess
    import time
    # A long grace, so waiting it out is unmistakable (and a loaded machine is not).
    pytester.makeini("[pytest]\nlanes_interrupt_grace = 30\n")
    pytester.makeconftest("""
        import os, signal, threading, time, pytest
        def pytest_runtest_logreport(report):   # a slow reporter, on the main thread
            if "test_excl" in report.nodeid and report.when == "call":
                threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
                time.sleep(1)
        @pytest.fixture(scope="module")
        def res():
            yield
            with open(os.environ["LANES_OUT"] + "/teardown.log", "a") as f:
                f.write("module teardown ran\\n")
    """)
    pytester.makepyfile("""
        import time, pytest
        @pytest.fixture
        def slow_td():
            yield
            time.sleep(0.5)
        @pytest.mark.lanes_exclusive
        def test_excl(res, slow_td):
            pass
        @pytest.mark.lanes_exclusive
        def test_excl2(res):
            pass
    """)
    env = {**os.environ, "LANES_OUT": str(pytester.path)}
    started = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "pytest", *BASE, "--lanes", "2"], cwd=pytester.path,
                       env=env, capture_output=True, text=True, timeout=60)
    took = time.monotonic() - started
    assert p.returncode == pytest.ExitCode.INTERRUPTED, p.stdout
    assert took < 15 and "did not stop" not in p.stdout, (took, p.stdout)
    assert (pytester.path / "teardown.log").exists(), p.stdout


def test_keyboard_interrupt_raised_by_a_test_stops_the_run(pytester):
    # Only the raising lane stopped: the other ran everything else, and tests queued to
    # the stopped lane silently never ran (round-5 review). xdist stops the run:
    # "1 failed, 2 passed", Interrupted.
    pytester.makepyfile("""
        import time, pytest
        def test_a_ki():
            raise KeyboardInterrupt
        @pytest.mark.parametrize("i", range(6))
        def test_b(i):
            time.sleep(0.5)
    """)
    r = run(pytester, "--lanes", "2", timeout=60)
    assert r.ret == pytest.ExitCode.INTERRUPTED, r.stdout.str()
    assert r.parseoutcomes().get("passed", 0) <= 2, r.stdout.str()


# ---------------------------------------------------------------- memory (round 5, cycle 2)
@pytest.mark.parametrize("mode", [["--lanes", "4"], ["-n", "1", "--lanes", "4"]], ids=["lanes", "hybrid"])
def test_fixture_definitions_do_not_accumulate(pytester, monkeypatch, mode):
    # pytest 9 makes a FixtureDef for `request` per test. Lanes kept per-lane fixture state
    # in a dict keyed by FixtureDef, so each of them (and what its cache held) lived for
    # the whole run: about 7 KB per test (soak run, 4,000 tests).
    monkeypatch.setenv("LANES_OUT", str(pytester.path))
    pytester.makeconftest("""
        import gc, os, pathlib
        from _pytest.fixtures import FixtureDef
        def pytest_sessionfinish(session):
            if not hasattr(session.config, "workerinput") or "." not in str(session.config.workerinput.get("workerid", "")):
                gc.collect()
                live = sum(isinstance(o, FixtureDef) for o in gc.get_objects())
                (pathlib.Path(os.environ["LANES_OUT"]) / f"live-{os.getpid()}").write_text(str(live))
    """)
    pytester.makepyfile("""
        import pytest
        @pytest.fixture
        def fx(request):
            return request.param if hasattr(request, "param") else 1
        @pytest.mark.parametrize("i", range(400))
        def test_t(i, fx, request, tmp_path):
            pass
    """)
    run(pytester, *mode, timeout=120).assert_outcomes(passed=400)
    counts = [int(p.read_text()) for p in pytester.path.glob("live-*")]
    assert counts and max(counts) < 100, counts


@pytest.mark.parametrize("mode", [["--lanes", "2"], ["-n", "1", "--lanes", "2"]], ids=["lanes", "hybrid"])
def test_keyboard_interrupt_with_a_slow_reporter(pytester, mode):
    # The lane-error check ran right after an event was dequeued and threw it away: a lost
    # "item done" left its lane waiting (grace period, no teardown), and in hybrid mode a
    # test was reported both passed and crashed (round-5 cycle-2 review).
    import time
    pytester.makeini("[pytest]\nlanes_interrupt_grace = 30\n")   # waiting it out is unmistakable
    pytester.makeconftest("""
        import time
        def pytest_runtest_logfinish(nodeid, location):
            if nodeid.endswith("test_b"):
                time.sleep(1.0)
    """)
    pytester.makepyfile("""
        import time
        def test_a():
            time.sleep(0.3)
            raise KeyboardInterrupt
        def test_b():
            pass
    """)
    started = time.monotonic()
    r = run(pytester, *mode, timeout=60)
    assert time.monotonic() - started < 15, r.stdout.str()
    assert r.ret == pytest.ExitCode.INTERRUPTED, r.stdout.str()
    out = r.stdout.str()
    assert "did not stop" not in out, out
    # xdist reports the test that raised it as a crashed worker; no other test may be.
    assert not any("crashed" in line and "test_b" in line for line in out.splitlines()), out


@pytest.mark.parametrize("mode", [["--lanes", "3"], ["-n", "2", "--lanes", "2"]], ids=["lanes", "hybrid"])
def test_error_in_a_reporter_still_tears_the_lanes_down(pytester, monkeypatch, mode):
    # An exception in a main-thread hook (a plugin's logreport, a custom scheduler) left
    # every lane without teardown; xdist's workers tear down (round-5 cycle-3 review).
    monkeypatch.setenv("LANES_OUT", str(pytester.path))
    pytester.makeconftest("""
        import os, pathlib, pytest
        OUT = pathlib.Path(os.environ["LANES_OUT"])
        def pytest_runtest_logreport(report):
            if report.when == "call" and "test_boom" in report.nodeid:
                raise RuntimeError("reporter bug")
        @pytest.fixture(scope="session", autouse=True)
        def session_env(worker_id):
            yield
            (OUT / f"session-{worker_id}").write_text("x")
    """)
    pytester.makepyfile("""
        import time, pytest
        def test_boom():
            time.sleep(0.3)
        @pytest.mark.parametrize("i", range(4))
        def test_slow(i):
            time.sleep(1.5)
    """)
    r = run(pytester, *mode, timeout=120)
    assert r.ret == pytest.ExitCode.INTERNAL_ERROR, r.stdout.str()
    lanes = 3 if mode[0] == "--lanes" else 4
    torn = list(pytester.path.glob("session-*"))
    assert len(torn) >= min(lanes, 5) - 1, (torn, r.stdout.str()[-1500:])


# ---------------------------------------------------------------- cycle-4 review
def test_maxfail_stops_lanes_queued_behind_an_exclusive_test(pytester, monkeypatch):
    # Lanes checked for -x before taking the exclusivity lock, never after: lanes queued
    # behind an exclusive test all started once a failure had stopped the run. Nothing
    # may start after the failure (tests that started before it may finish).
    monkeypatch.setenv("LANES_OUT", str(pytester.path))
    from test_contract import CUSTOM_SCHED
    pytester.makeconftest(CUSTOM_SCHED)
    pytester.makepyfile("""
        import os, pathlib, time, pytest
        OUT = pathlib.Path(os.environ["LANES_OUT"])
        def mark(name):
            (OUT / f"start-{name}").write_text(repr(time.time()))
        @pytest.mark.parametrize("env", ["env0"])
        def test_fail(env):
            time.sleep(1.0)
            (OUT / "failed-at").write_text(repr(time.time()))
            assert 0
        @pytest.mark.lanes_exclusive
        @pytest.mark.parametrize("env", ["env1"])
        def test_excl(env):
            mark("excl")
        @pytest.mark.parametrize("env", ["env2-0", "env2-1"])
        def test_other(env):
            mark(env)
            time.sleep(0.3)
    """)
    for _ in range(3):
        for p in pytester.path.glob("start-*"):
            p.unlink()
        r = run(pytester, "-n", "1", "--lanes", "3", "-x", timeout=60)
        failed_at = float((pytester.path / "failed-at").read_text())
        late = [p.name for p in pytester.path.glob("start-*") if float(p.read_text()) > failed_at]
        assert late == [], (late, r.stdout.str())


@pytest.mark.parametrize("mode", [["--lanes", "3"], ["-n", "2", "--lanes", "2"]], ids=["lanes", "hybrid"])
def test_pytest_exit_tears_down_every_lane(pytester, monkeypatch, mode):
    # pytest.exit() in a test left that lane's fixtures set up (only KeyboardInterrupt
    # was handled): its session fixture never tore down.
    monkeypatch.setenv("LANES_OUT", str(pytester.path))
    pytester.makeconftest("""
        import os, pathlib, pytest
        @pytest.fixture(scope="session", autouse=True)
        def env(worker_id):
            yield
            (pathlib.Path(os.environ["LANES_OUT"]) / f"torn-{worker_id}").write_text("")
    """)
    pytester.makepyfile("""
        import time, pytest
        def test_a():
            time.sleep(0.2)
            pytest.exit("stop")
        def test_b():
            time.sleep(0.5)
    """)
    run(pytester, *mode, timeout=60)
    torn = sorted(p.name for p in pytester.path.glob("torn-*"))
    assert len(torn) == 2, torn


# ---------------------------------------------------------------- cycle-5 review (hybrid)
def test_worker_crash_does_not_end_the_run(pytester, monkeypatch):
    # Removing a dead worker's lanes one by one let xdist reschedule their tests onto the
    # dead worker's other lanes; sending to them raised OSError, the run ended in
    # INTERNALERROR and most tests never ran (82 of 600).
    pytester.makepyfile("""
        import os, time, pytest
        @pytest.mark.parametrize("i", range(300))
        def test_t(i, tmp_path_factory):
            flag = tmp_path_factory.getbasetemp().parent / "crashed"
            time.sleep(0.05)
            if i == 40 and not flag.exists():
                flag.write_text("x")
                os._exit(1)
    """)
    r = run(pytester, "-n", "2", "--lanes", "2", "--dist", "load", timeout=180)
    out = r.stdout.str()
    assert "INTERNALERROR" not in out, out[-3000:]
    assert r.ret == pytest.ExitCode.TESTS_FAILED, out[-2000:]
    assert r.parseoutcomes().get("passed", 0) >= 290, out[-2000:]


def test_workeroutput_written_on_a_lane_reaches_the_controller(pytester):
    # Each lane wrote to a private workeroutput that never reached the controller
    # (pytest_testnodedown); a hybrid worker's lanes now share the process's, as an
    # xdist worker's tests do.
    pytester.makeconftest("""
        import pytest
        @pytest.fixture(scope="session", autouse=True)
        def note(request):
            yield
            request.config.workeroutput.setdefault("ran", []).append(request.config.workerinput["workerid"])
        def pytest_testnodedown(node, error):
            print("NODEDOWN", node.gateway.id, sorted(node.workeroutput.get("ran", ["<missing>"])))
    """)
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("i", range(4))
        def test_t(i):
            pass
    """)
    r = run(pytester, "-n", "1", "--lanes", "2", "-s", timeout=60)
    r.stdout.fnmatch_lines(["*NODEDOWN gw0 ['gw0.ln*"])


def test_lanes_dist_is_refused_in_hybrid_mode(pytester):
    pytester.makepyfile("def test_t(): pass\n")
    r = run(pytester, "-n", "2", "--lanes", "2", "--lanes-dist", "loadfile", timeout=60)
    assert r.ret == pytest.ExitCode.USAGE_ERROR, r.stdout.str()
    r.stderr.fnmatch_lines(["*--lanes-dist*--dist*"])


def test_subclass_of_an_unsupported_scheduler_is_refused_up_front(pytester):
    pytester.makeconftest("""
        from xdist.scheduler import WorkStealingScheduling
        class Mine(WorkStealingScheduling):
            pass
        def pytest_xdist_make_scheduler(config, log):
            return Mine(config, log)
    """)
    pytester.makepyfile("def test_t(): pass\n")
    r = run(pytester, "--lanes", "2", timeout=60)
    assert r.ret == pytest.ExitCode.USAGE_ERROR, r.stdout.str() + r.stderr.str()


def test_collect_only_with_a_collection_error_exits_interrupted(pytester):
    # xdist and plain pytest end "Interrupted: 1 error during collection" (exit 2);
    # single-process lanes returned early and exited 1 (cycle-6 review).
    pytester.makepyfile(test_ok="def test_t(): pass\n", test_bad="import nonexistent_module_xyz\n")
    r = run(pytester, "--lanes", "3", "--co", timeout=60)
    assert r.ret == pytest.ExitCode.INTERRUPTED, r.stdout.str()


@pytest.mark.parametrize("mode", [["--lanes", "3"], ["-n", "2", "--lanes", "2"]], ids=["lanes", "hybrid"])
def test_no_dead_symlinks_left_in_lane_basetemps(pytester, mode):
    # pytest removes dangling "<name>current" links from its basetemp when retention drops
    # directories; lanes' own basetemps kept them (cycle-6 review).
    pytester.makeini("[pytest]\ntmp_path_retention_policy = failed\n")
    pytester.makepyfile("""
        import pytest, time
        @pytest.mark.parametrize("i", range(6))
        def test_t(tmp_path, i):
            (tmp_path / "f").write_text("x"); time.sleep(0.05)
            assert i != 5
    """)
    bt = pytester.path / "bt"
    run(pytester, *mode, f"--basetemp={bt}", timeout=60)
    dead = [p for p in bt.rglob("*") if p.is_symlink() and not p.exists()]
    assert dead == [], dead

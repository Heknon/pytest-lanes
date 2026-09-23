"""Robustness: failure paths, odd options and odd run shapes must end cleanly, like xdist.

"Cleanly" means: no hang (every run here has a timeout), no INTERNALERROR that
xdist would not also raise, and the same exit code as plain xdist.
"""
import inspect

import _pytest.runner
import pytest
from lanes_testing import MODES, run

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

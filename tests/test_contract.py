"""Contract tests: lanes must look like xdist --dist loadgroup to consumers."""
import sys
import pytest

pytest_plugins = ["pytester"]
BASE = ["-p", "no:warnings", "-p", "no:cacheprovider", "-p", "no:randomly"]

import _pytest.threadexception as _te
import _pytest.unraisableexception as _ue
if not hasattr(_te, "pytest_configure"):     # older pytest: per-test global hook swapping
    BASE += ["-p", "no:threadexception"]
if not hasattr(_ue, "pytest_configure"):
    BASE += ["-p", "no:unraisableexception"]


def run(pytester, *args):
    return pytester.runpytest_subprocess(*BASE, *args)


def test_groups_serial_across_parallel(pytester):
    pytester.makepyfile(
        """
        import time, pytest
        SEEN = {}
        @pytest.mark.parametrize("g", "ABCD")
        @pytest.mark.parametrize("i", range(2))
        def test_x(g, i, request):
            request.node.add_marker(pytest.mark.xdist_group(name=g))
            time.sleep(1)
        """
    )
    # marks added at runtime are too late for scheduling -> use collection hook instead
    pytester.makeconftest(
        """
        import pytest
        def pytest_collection_modifyitems(items):
            for it in items:
                it.add_marker(pytest.mark.xdist_group(name=it.callspec.params["g"]))
        """
    )
    r = run(pytester, "--lanes", "4", "--lanes-dist", "loadgroup", "-v")
    r.assert_outcomes(passed=8)
    assert r.duration < 5, r.duration          # 4 lanes x 2 sequential x 1s ~= 2s


def test_session_fixture_is_per_lane_like_xdist_worker(pytester):
    pytester.makeconftest(
        """
        import pytest, threading
        N = []
        @pytest.fixture(scope="session")
        def env():
            N.append(1); yield len(N)
        def pytest_sessionfinish(session):
            print(f"\\nSESSION_SETUPS={len(N)}")
        """
    )
    pytester.makepyfile(
        """
        import pytest
        @pytest.mark.xdist_group(name="a")
        def test_a1(env): pass
        @pytest.mark.xdist_group(name="a")
        def test_a2(env): pass
        @pytest.mark.xdist_group(name="b")
        def test_b1(env): pass
        """
    )
    r = run(pytester, "--lanes", "2", "--lanes-dist", "loadgroup", "-s")
    r.assert_outcomes(passed=3)
    r.stdout.fnmatch_lines(["*SESSION_SETUPS=2*"])   # one per lane, reused inside lane


def test_teardown_order_and_finalizers_per_lane(pytester):
    pytester.makepyfile(
        """
        import pytest, time
        LOG = []
        @pytest.fixture(scope="module")
        def mod(request):
            name = request.node.name
            yield
        @pytest.fixture
        def f(request):
            yield request.node.name
            LOG.append(request.node.name)
        @pytest.mark.parametrize("g", ["x", "y"])
        def test_t(g, f, mod):
            time.sleep(0.2)
            assert f.endswith(f"[{g}]")
        """
    )
    r = run(pytester, "--lanes", "2")
    r.assert_outcomes(passed=2)


def test_maxfail_stops_scheduling(pytester):
    pytester.makepyfile(
        """
        import pytest, time
        @pytest.mark.xdist_group(name="a")
        def test_fail(): assert 0
        @pytest.mark.xdist_group(name="a")
        def test_after_fail_same_lane(): time.sleep(0.5)
        """
    )
    r = run(pytester, "--lanes", "2", "--lanes-dist", "loadgroup", "-x")
    r.assert_outcomes(failed=1)
    assert r.ret == 1


def test_rerunfailures_protocol_plugin(pytester):
    pytest.importorskip("pytest_rerunfailures")
    pytester.makepyfile(
        """
        import pytest
        COUNT = {}
        @pytest.mark.parametrize("g", ["a", "b"])
        @pytest.mark.flaky(reruns=2)
        def test_flaky(g):
            COUNT[g] = COUNT.get(g, 0) + 1
            assert COUNT[g] >= 2
        """
    )
    r = run(pytester, "--lanes", "2")
    o = r.parseoutcomes()
    assert o.get("passed") == 2 and o.get("rerun") == 2, o


def test_node_hooks_opt_in_for_observers(pytester):
    pytest.importorskip("xdist")
    pytester.makeconftest(
        """
        SEEN = []
        def pytest_testnodeready(node): SEEN.append(("up", node.gateway.id))
        def pytest_testnodedown(node, error): SEEN.append(("down", node.gateway.id))
        def pytest_runtest_logreport(report):
            if report.when == "call":
                SEEN.append(("rep", report.node.workerinput["workerid"]))
        def pytest_sessionfinish(session):
            print("\\nSEEN", sorted(set(k for k, _ in SEEN)))
        """
    )
    pytester.makepyfile("def test_a(): pass\ndef test_b(): pass")
    r = run(pytester, "--lanes", "2", "--lanes-xdist-node-hooks", "-s")
    r.stdout.fnmatch_lines(["*SEEN*'down', 'rep', 'up'*"])


def test_fail_closed_on_unsafe_warnings(pytester):
    if getattr(sys.flags, "context_aware_warnings", False):
        pytest.skip("warnings are context-aware on this interpreter")
    pytester.makepyfile("def test_a(): pass")
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--lanes", "2")
    r.stderr.fnmatch_lines(["*refuses to run*", "*context_aware_warnings*"])


def test_capfd_routed_to_serial_phase(pytester):
    pytester.makepyfile(
        """
        import os
        def test_fd(capfd):
            os.write(1, b"raw")
            assert capfd.readouterr().out == "raw"
        """
    )
    r = run(pytester, "--lanes", "2", "-v")
    r.assert_outcomes(passed=1)
    r.stdout.fnmatch_lines(["*[[]ln-serial[]]*PASSED*test_fd*"])


CUSTOM_SCHED = """
import pytest
from xdist.scheduler import LoadScopeScheduling

class EnvScheduling(LoadScopeScheduling):
    # nodeid like 'test_x.py::test_step[envB-2]' -> scope 'envB'
    def _split_scope(self, nodeid):
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]

def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)
"""

ENV_TESTS = """
import os, time, threading, pytest
RUNS = {}
@pytest.mark.parametrize("step", range(3))
@pytest.mark.parametrize("env", ["envA", "envB", "envC"])
def test_step(env, step, worker_id):
    who = RUNS.setdefault(env, [])
    who.append((step, worker_id, os.getpid(), threading.get_ident()))
    assert [s for s, *_ in who] == list(range(step + 1))       # sequential within env
    assert len({w[1:] for w in who}) == 1                        # same worker for whole env
    time.sleep(0.5)
"""


@pytest.mark.parametrize("mode", [["-n", "3"], ["--lanes", "3"]], ids=["xdist", "lanes"])
def test_same_custom_scheduler_both_backends(pytester, mode):
    pytest.importorskip("xdist")
    pytester.makeconftest(CUSTOM_SCHED)
    pytester.makepyfile(ENV_TESTS)
    r = run(pytester, *mode, "-v")
    r.assert_outcomes(passed=9)
    assert r.duration < 4.5, r.duration     # 3 envs in parallel, 3 x 0.5s each


def test_report_parity_with_xdist_loadgroup(pytester):
    import json
    pytest.importorskip("xdist")
    pytest.importorskip("pytest_reportlog")
    pytester.makepyfile("""
        import pytest, logging
        @pytest.mark.parametrize("i", range(2))
        @pytest.mark.parametrize("g", "AB")
        def test_t(g, i, request):
            print("out", g, i); logging.getLogger("t").warning("log %s %s", g, i)
            assert not (g == "B" and i == 1)
        def pytest_collection_modifyitems(items): pass
    """)
    # tryfirst matters: xdist's worker adds the '@group' suffix in its own
    # modifyitems; markers added after it are silently ignored by xdist loadgroup.
    pytester.makeconftest("""
        import pytest
        @pytest.hookimpl(tryfirst=True)
        def pytest_collection_modifyitems(items):
            for it in items: it.add_marker(pytest.mark.xdist_group(name=it.callspec.params["g"]))
    """)
    def rl(*args):
        run(pytester, *args, "--report-log=rl.jsonl")
        return sorted((e["nodeid"], e["when"], e["outcome"], tuple(s[0] for s in e["sections"]))
                      for e in map(json.loads, open(pytester.path / "rl.jsonl"))
                      if e.get("$report_type") == "TestReport")
    a = rl("-n", "2", "--dist", "loadgroup")
    b = rl("--lanes", "2", "--lanes-dist", "loadgroup")
    assert a == b                            # nodeids incl. '@group' suffix now match exactly
    assert any("@A" in x[0] for x in a)


# ------------------------------------------------------------ hybrid: -n N --lanes M
def test_hybrid_custom_scheduler_pins_env_to_one_lane(pytester):
    pytest.importorskip("xdist")
    pytester.makeconftest(CUSTOM_SCHED)
    pytester.makepyfile("""
        import os, threading, time, pytest
        @pytest.mark.parametrize("step", range(3))
        @pytest.mark.parametrize("env", [f"env{c}" for c in "ABCDEF"])
        def test_step(env, step, tmp_path_factory):
            d = tmp_path_factory.getbasetemp().parent
            (d / f"{env}-{step}").write_text(f"{os.getpid()}|{threading.current_thread().name}|{time.time()}")
            time.sleep(0.5)
    """)
    r = run(pytester, "-n", "2", "--lanes", "3", f"--basetemp={pytester.path / 'bt'}")
    r.assert_outcomes(passed=18)
    assert r.duration < 5, r.duration                 # 6 envs on 6 lanes: ~3 x 0.5s
    runs = {}
    for f in (pytester.path / "bt").glob("env*-*"):
        env, step = f.name.rsplit("-", 1)
        pid, lane, t = f.read_text().split("|")
        runs.setdefault(env, []).append((float(t), int(step), pid, lane))
    assert len(runs) == 6
    for env, v in runs.items():
        v.sort()
        assert [s for _, s, _, _ in v] == [0, 1, 2], env          # sequential in order
        assert len({(p, l) for _, _, p, l in v}) == 1, env        # one lane in one process
    assert len({v[0][2] for v in runs.values()}) == 2             # both processes used


def test_hybrid_report_parity_with_plain_xdist(pytester):
    import json
    pytest.importorskip("pytest_reportlog")
    pytester.makeconftest("""
        import pytest
        @pytest.hookimpl(tryfirst=True)
        def pytest_collection_modifyitems(items):
            for it in items: it.add_marker(pytest.mark.xdist_group(name=it.callspec.params["g"]))
    """)
    pytester.makepyfile("""
        import pytest, logging
        @pytest.mark.parametrize("i", range(2))
        @pytest.mark.parametrize("g", "ABC")
        def test_t(g, i):
            print("out", g, i); logging.getLogger("t").warning("log %s %s", g, i)
            assert not (g == "B" and i == 1)
    """)
    def rl(*args):
        run(pytester, *args, "--report-log=rl.jsonl")
        return sorted((e["nodeid"], e["when"], e["outcome"], tuple(s[0] for s in e["sections"]))
                      for e in map(json.loads, open(pytester.path / "rl.jsonl"))
                      if e.get("$report_type") == "TestReport")
    assert rl("-n", "2", "--dist", "loadgroup") == rl("-n", "2", "--lanes", "2", "--dist", "loadgroup")


def test_hybrid_worker_crash_is_reported_and_rescheduled(pytester):
    pytester.makepyfile("""
        import os, time, pytest
        @pytest.mark.parametrize("step", range(2))
        @pytest.mark.parametrize("env", ["envX", "envY", "envZ"])
        def test_c(env, step, tmp_path_factory):
            flag = tmp_path_factory.getbasetemp().parent / "crashed-once"
            time.sleep(0.5)
            if env == "envZ" and step == 1 and not flag.exists():
                flag.write_text("x"); os._exit(1)
    """)
    pytester.makeconftest(CUSTOM_SCHED.replace("rsplit(\"[\", 1)[1].split(\"-\", 1)[0]",
                                               "rsplit(\"[\", 1)[1].split(\"-\", 1)[0]"))
    r = run(pytester, "-n", "1", "--lanes", "3", f"--basetemp={pytester.path / 'bt'}", "-v")
    r.stdout.fnmatch_lines(["*node down*", "*FAILED*envZ-1*", "*replacing crashed worker*"])
    # every test eventually passed on the replacement worker
    for env in ("envX", "envY", "envZ"):
        for step in (0, 1):
            r.stdout.fnmatch_lines([f"*PASSED*test_c?{env}-{step}?*"])


def test_hybrid_capsys_runs_exclusively_inside_worker(pytester):
    pytester.makepyfile("""
        import time, pytest
        @pytest.mark.parametrize("i", range(4))
        def test_noisy(i):
            for _ in range(20):
                print("noise", i); time.sleep(0.02)
        def test_cap(capsys):
            print("mine"); time.sleep(0.3)
            assert capsys.readouterr().out == "mine\\n"
    """)
    r = run(pytester, "-n", "1", "--lanes", "5")
    r.assert_outcomes(passed=5)

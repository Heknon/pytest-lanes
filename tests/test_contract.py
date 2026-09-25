"""Contract tests: lanes must look like xdist --dist loadgroup to consumers."""
import json
import re
import sys

import pytest
from lanes_testing import BASE, max_overlap, report_log, run, stamp_pids, stamps_dir


def test_groups_serial_across_parallel(pytester, monkeypatch):
    stamps = stamps_dir(pytester, monkeypatch)
    pytester.makepyfile(
        """
        import time, pytest
        from stamping import stamped
        SEEN = {}
        @pytest.mark.parametrize("g", "ABCD")
        @pytest.mark.parametrize("i", range(2))
        def test_x(g, i, request):
            request.node.add_marker(pytest.mark.xdist_group(name=g))
            with stamped(request):
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
    assert max_overlap(stamps) == 4           # the 4 groups ran in parallel, 2 tests each


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
    assert r.ret == pytest.ExitCode.INTERRUPTED     # as xdist


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


def test_fail_closed_on_unsafe_warnings(pytester, monkeypatch):
    # Free-threaded 3.14t defaults context_aware_warnings on; force it off (ignored < 3.14).
    monkeypatch.setenv("PYTHON_CONTEXT_AWARE_WARNINGS", "0")
    pytester.makepyfile("def test_a(): pass")
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--lanes", "2")
    r.stderr.fnmatch_lines(["*refuses to run*", "*context_aware_warnings*"])


WARN_TESTS = """
import time, warnings, pytest
@pytest.mark.parametrize("i", range(3))
def test_warns(i):
    time.sleep(0.3)                          # overlap with the filtered tests below
    warnings.warn(f"w{i}", UserWarning)
@pytest.mark.filterwarnings("error")
def test_error():
    time.sleep(0.2)
    warnings.warn("boom", UserWarning)       # must fail this test only
@pytest.mark.filterwarnings("ignore")
def test_ignored():
    time.sleep(0.2)
    warnings.warn("hidden", UserWarning)     # must not be recorded, nor hide the others
"""
WARN_CONFTEST = """
import threading
SEEN = []
def pytest_warning_recorded(warning_message, when, nodeid):
    assert threading.current_thread() is threading.main_thread()   # routed like xdist
    SEEN.append(f"{nodeid.split('::')[-1]}={warning_message.message}")
def pytest_sessionfinish(session):
    print("\\nWARNINGS", sorted(SEEN))
"""


def test_warnings_captured_per_test_on_context_aware_interpreter(pytester, monkeypatch):
    if sys.version_info < (3, 14):
        pytest.skip("needs -X context_aware_warnings (Python 3.14+)")
    monkeypatch.setenv("PYTHON_CONTEXT_AWARE_WARNINGS", "1")
    pytester.makeconftest(WARN_CONFTEST)
    pytester.makepyfile(WARN_TESTS)
    base = [a for a in BASE if a != "no:warnings"]
    base.remove("-p")                         # the one preceding "no:warnings"
    out = {}
    for mode in (["-n", "5"], ["--lanes", "5"], ["-n", "1", "--lanes", "5"]):
        r = pytester.runpytest_subprocess(*base, *mode, "-rf")
        r.assert_outcomes(passed=4, failed=1, warnings=3)
        r.stdout.fnmatch_lines(["E  *UserWarning: boom", "FAILED *::test_error*"])
        out[" ".join(mode)] = [ln for ln in r.outlines if ln.startswith("WARNINGS")]
    expected = ["WARNINGS ['test_warns[0]=w0', 'test_warns[1]=w1', 'test_warns[2]=w2']"]
    assert out == {k: expected for k in out}, out


def test_tmp_path_basetemp_created_once_across_lanes(pytester):
    # pytest creates basetemp lazily, without a lock. With --basetemp (which xdist always
    # sets on workers) the first tmp_path of two lanes could both rmtree+mkdir it.
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("i", range(32))
        def test_t(i, tmp_path):
            (tmp_path / "f").write_text(str(i))
    """)
    r = run(pytester, "--lanes", "32", f"--basetemp={pytester.path / 'bt'}")
    r.assert_outcomes(passed=32)
    assert len(list((pytester.path / "bt").glob("ln*/test_t_*_0/f"))) == 32   # per-lane basetemps


def test_current_test_env_var_survives_concurrent_teardown(pytester):
    # os.environ.pop(k, None) is check-then-delete (MutableMapping.pop), so two lanes
    # finishing together could still raise KeyError: 'PYTEST_CURRENT_TEST' (P6).
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("i", range(600))
        def test_t(i): pass
    """)
    r = run(pytester, "--lanes", "32")
    r.assert_outcomes(passed=600)


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
    # The nodeid may come before or after the result, on the same line or not.
    out = r.stdout.str()
    assert re.search(r"\[ln-serial\].*PASSED", out) and "test_fd" in out, out


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
from stamping import stamped
RUNS = {}
@pytest.mark.parametrize("step", range(3))
@pytest.mark.parametrize("env", ["envA", "envB", "envC"])
def test_step(env, step, worker_id, request):
    who = RUNS.setdefault(env, [])
    who.append((step, worker_id, os.getpid(), threading.get_ident()))
    assert [s for s, *_ in who] == list(range(step + 1))       # sequential within env
    assert len({w[1:] for w in who}) == 1                        # same worker for whole env
    with stamped(request):
        time.sleep(0.5)
"""


@pytest.mark.parametrize("mode", [["-n", "3"], ["--lanes", "3"]], ids=["xdist", "lanes"])
def test_same_custom_scheduler_both_backends(pytester, monkeypatch, mode):
    pytest.importorskip("xdist")
    stamps = stamps_dir(pytester, monkeypatch)
    pytester.makeconftest(CUSTOM_SCHED)
    pytester.makepyfile(ENV_TESTS)
    r = run(pytester, *mode, "-v")
    r.assert_outcomes(passed=9)
    assert max_overlap(stamps) == 3         # the 3 envs ran in parallel, each in order


NODE_INSPECTING_SCHED = """
from xdist.scheduler import LoadScopeScheduling

class InspectingScheduling(LoadScopeScheduling):
    # A custom scheduler may read what xdist's WorkerController offers, e.g. for logging.
    def add_node(self, node):
        wi = node.workerinput
        print(f"\\nNODE {node.gateway.id} workerid={wi['workerid']} count={wi['workercount']} "
              f"uid={wi['testrunuid']} info={node.workerinfo['id']}", flush=True)
        super().add_node(node)

def pytest_xdist_make_scheduler(config, log):
    return InspectingScheduling(config, log)
"""


@pytest.mark.parametrize("mode,total", [(["-n", "2"], 2), (["--lanes", "4"], 4),
                                        (["-n", "2", "--lanes", "2"], 4)],
                         ids=["xdist", "lanes", "hybrid"])
def test_scheduler_sees_worker_shaped_nodes(pytester, mode, total):
    # Every node handed to the scheduler must carry the attributes of an xdist worker.
    pytester.makeconftest(NODE_INSPECTING_SCHED)
    pytester.makepyfile("import pytest\n@pytest.mark.parametrize('i', range(4))\ndef test_t(i): pass")
    r = run(pytester, *mode, "-s")
    r.assert_outcomes(passed=4)
    import re
    nodes = [re.match(r"NODE (\S+) workerid=(\S+) count=(\d+) uid=(\S+) info=(\S+)", ln).groups()
             for ln in r.outlines if ln.startswith("NODE ")]
    assert len(nodes) == total, r.outlines
    for gid, workerid, count, _, info in nodes:
        assert workerid == gid == info and int(count) == total, nodes
    assert len({uid for *_, uid, _ in nodes}) == 1, nodes    # one test run


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
def test_hybrid_custom_scheduler_pins_env_to_one_lane(pytester, monkeypatch):
    pytest.importorskip("xdist")
    stamps = stamps_dir(pytester, monkeypatch)
    pytester.makeconftest(CUSTOM_SCHED)
    pytester.makepyfile("""
        import os, threading, time, pytest
        from stamping import stamped
        @pytest.mark.parametrize("step", range(3))
        @pytest.mark.parametrize("env", [f"env{c}" for c in "ABCDEF"])
        def test_step(env, step, tmp_path_factory, request):
            d = tmp_path_factory.getbasetemp().parent
            (d / f"{env}-{step}").write_text(f"{os.getpid()}|{threading.current_thread().name}|{time.time()}")
            with stamped(request):
                time.sleep(0.5)
    """)
    r = run(pytester, "-n", "2", "--lanes", "3", f"--basetemp={pytester.path / 'bt'}")
    r.assert_outcomes(passed=18)
    # 6 envs on 6 lanes: each process ran its 3 lanes at once
    assert [max_overlap(stamps, pid) for pid in sorted(stamp_pids(stamps))] == [3, 3]
    runs = {}
    for f in (pytester.path / "bt").glob("env*-*"):
        env, step = f.name.rsplit("-", 1)
        pid, lane, t = f.read_text().split("|")
        runs.setdefault(env, []).append((float(t), int(step), pid, lane))
    assert len(runs) == 6
    for env, v in runs.items():
        v.sort()
        assert [s for _, s, _, _ in v] == [0, 1, 2], env          # sequential in order
        assert len({(pid, lane) for _, _, pid, lane in v}) == 1, env   # one lane in one process
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


def test_hybrid_crash_collateral_is_reported_and_rerun_under_loadscope(pytester):
    # A loadscope-based scheduler (the user's EnvScheduling) requeues a crashed node's
    # unfinished environments whole, starting at the step that was running: plain xdist
    # reports its crashed test failed and runs it again. In hybrid mode the sibling lane's
    # in-flight test (collateral) gets the same treatment, and every environment's steps
    # still run in order, on one lane (round 7; DESIGN.md F5).
    pytester.makeconftest(CUSTOM_SCHED)
    pytester.makepyfile("""
        import os, time, pytest
        @pytest.mark.parametrize("env,step", [(e, i) for e in ("envP", "envQ") for i in range(3)],
                                 ids=[f"{e}-{i}" for e in ("envP", "envQ") for i in range(3)])
        def test_c(env, step, worker_id, tmp_path_factory):
            root = tmp_path_factory.getbasetemp().parent
            with open(root / "log", "a") as f:
                f.write(f"{worker_id} {env}-{step}\\n")
            if (env, step) == ("envQ", 1) and not (root / "crashed").exists():
                time.sleep(0.3)                  # envP-0 is running on the other lane
                (root / "crashed").write_text("x"); os._exit(1)
            time.sleep(1 if env == "envP" else 0.05)
    """)
    result, _ = report_log(pytester, "-n", "1", "--lanes", "2", f"--basetemp={pytester.path / 'bt'}")
    outcomes = {}
    for e in map(json.loads, open(pytester.path / "rl.jsonl")):
        if e.get("$report_type") == "TestReport" and e["when"] in ("call", "???"):  # ???: xdist's crash report
            outcomes.setdefault(e["nodeid"].split("[")[1][:-1], []).append(e["outcome"])
    assert outcomes["envQ-1"] == ["failed", "passed"], outcomes      # the culprit, as in xdist
    assert outcomes["envP-0"] == ["failed", "passed"], outcomes      # the collateral (in flight)
    assert all(v == ["passed"] for k, v in outcomes.items() if k not in ("envQ-1", "envP-0")), outcomes
    runs = {}
    for line in (pytester.path / "bt" / "log").read_text().splitlines():
        worker, name = line.split()
        env, step = name.split("-")
        runs.setdefault((env, worker.split(".")[0]), []).append((int(step), worker))
    for (env, _), steps in runs.items():                 # in order, one lane, per process
        assert [s for s, _ in steps] == sorted(s for s, _ in steps), runs
        assert len({w for _, w in steps}) == 1, runs


#: The scheduler README.md recommends, verbatim: a lane gets its next environment only
#: when it has at most one test left (the one a worker holds back until it knows what
#: comes next). xdist's loadscope tops a node up at two, so an environment could wait
#: behind two tests of another while other lanes sat idle.
RECOMMENDED_SCHED = """
from xdist.scheduler import LoadScopeScheduling

class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid):     # 'test_x.py::test_step[envB-2]' -> 'envB'
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]

    def _reschedule(self, node):
        # Queue the next environment only behind the lane's last test, not its last two.
        if node.shutting_down or not self.workqueue or self._pending_of(self.assigned_work[node]) <= 1:
            super()._reschedule(node)

def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)
"""


@pytest.mark.parametrize("mode", [["-n", "2"], ["--lanes", "2"], ["-n", "1", "--lanes", "2"]],
                         ids=["xdist", "lanes", "hybrid"])
def test_recommended_scheduler_does_not_queue_an_environment_behind_another(pytester, monkeypatch, mode):
    # envA: a short step, then two long ones. With stock loadscope, envC is queued on
    # envA's lane as soon as envA has two tests left, and waits there while the other
    # lane finishes envB and sits idle (round 7, DESIGN.md item 50).
    stamps = stamps_dir(pytester, monkeypatch)
    pytester.makeconftest(RECOMMENDED_SCHED)
    pytester.makepyfile("""
        import time, pytest
        from stamping import stamped
        PLAN = {"envA": [0.05, 1.0, 1.0], "envB": [0.3, 0.3, 0.3], "envC": [0.3]}
        @pytest.mark.parametrize("env,step", [(e, i) for e, d in PLAN.items() for i in range(len(d))],
                                 ids=[f"{e}-{i}" for e, d in PLAN.items() for i in range(len(d))])
        def test_step(env, step, request):
            with stamped(request):
                time.sleep(PLAN[env][step])
    """)
    run(pytester, *mode, timeout=60).assert_outcomes(passed=7)
    times = {f.name.split("[")[1].rstrip("]"): tuple(map(float, f.read_text().split()[:2]))
             for f in stamps.iterdir()}
    assert times["envC-0"][0] < times["envA-2"][1], times     # envC did not wait for envA


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

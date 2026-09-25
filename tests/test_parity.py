"""Report parity: for the same suite, what consumers see must not depend on the mode.

Every test here runs one suite under plain xdist, single-process lanes and hybrid,
and compares report-log rows (nodeid, when, outcome, section names) or junitxml.
"""
import json
import re

import pytest
from lanes_testing import ENV_SCHED, MODES, dist_args, report_log, run

ALL_OUTCOMES_CONFTEST = """
import pytest
@pytest.fixture(scope="module")
def modfix(): yield "m"
@pytest.fixture
def bad_setup(): raise RuntimeError("setup err")
@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError("teardown err")
"""
ALL_OUTCOMES = """
import logging, sys, unittest, pytest
def test_pass(modfix):
    print("out"); sys.stderr.write("err\\n"); logging.getLogger("x").warning("log")
def test_fail(): assert 1 == 2
def test_skip(): pytest.skip("nope")
@pytest.mark.skip(reason="marker")
def test_skipmark(): pass
@pytest.mark.xfail(reason="known")
def test_xfail(): assert 0
@pytest.mark.xfail(reason="unexpectedly ok")
def test_xpass(): pass
@pytest.mark.xfail(strict=True)
def test_xpass_strict(): pass
def test_setup_error(bad_setup): pass
def test_teardown_error(bad_teardown): pass
@pytest.mark.parametrize("v", ["a-b", "x[y]", "\\u00fcn\\u00ef", "s p", "a::b", "@at", "", None, 1.5])
def test_ids(v): pass
class TestCls:
    def test_m(self, modfix): pass
    class TestNested:
        def test_n(self): pass
class UT(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.x = 1
    def test_u1(self): self.assertEqual(self.x, 1)
    def test_u2(self): self.fail("ut fail")
    @unittest.skip("ut skip")
    def test_u3(self): pass
def helper():
    '''
    >>> 1 + 1
    2
    >>> 1 + 1
    3
    '''
"""


@pytest.mark.parametrize("dist", ["load", "loadscope", "loadfile", "loadgroup"])
def test_every_outcome_kind_has_parity_under_every_dist_mode(pytester, dist):
    pytester.makeconftest(ALL_OUTCOMES_CONFTEST)
    pytester.makepyfile(test_kinds=ALL_OUTCOMES)
    pytester.mkpydir("sub").joinpath("test_more.py").write_text(
        "import pytest\n@pytest.mark.parametrize('i', range(5))\ndef test_more(i): pass\n")
    rows = {}
    for name, mode in MODES.items():
        result, rows[name] = report_log(pytester, *dist_args(mode, dist), "--doctest-modules")
        result.assert_outcomes(passed=19, failed=4, skipped=3, xfailed=1, xpassed=1, errors=2)
    assert rows["lanes"] == rows["xdist"]
    assert rows["hybrid"] == rows["xdist"]


def test_junitxml_parity(pytester):
    pytester.makepyfile("""
        import logging, pytest
        @pytest.mark.parametrize("i", range(4))
        def test_t(i, record_property):
            record_property("i", i); print("o", i); logging.getLogger("x").warning("w %s", i)
            assert i != 2
    """)

    def testcases(mode):
        run(pytester, *mode, "--junitxml=j.xml")
        xml = (pytester.path / "j.xml").read_text()
        xml = re.sub(r' (time|timestamp|hostname)="[^"]*"', "", xml)
        xml = re.sub(r"0x[0-9a-f]+", "0xADDR", xml)          # reprs in tracebacks
        return sorted(re.findall(r"<testcase .*?</testcase>|<testcase [^>]*/>", xml, re.S))

    cases = {name: testcases(mode) for name, mode in MODES.items()}
    assert len(cases["xdist"]) == 4
    assert cases["lanes"] == cases["xdist"]
    assert cases["hybrid"] == cases["xdist"]


def test_large_output_without_trailing_newline_has_parity(pytester):
    pytester.makepyfile("""
        import sys, pytest
        @pytest.mark.parametrize("i", range(3))
        def test_big(i):
            sys.stdout.write("x" * 2_000_000); print("tail", end="")
    """)
    results = {name: report_log(pytester, *mode) for name, mode in MODES.items()}
    for result, _ in results.values():
        result.assert_outcomes(passed=3)
    rows = {name: rows for name, (_, rows) in results.items()}
    assert rows["lanes"] == rows["xdist"] == rows["hybrid"]


def test_rerunfailures_has_parity_in_every_mode(pytester):
    pytest.importorskip("pytest_rerunfailures")
    pytester.makepyfile("""
        import pytest
        COUNT = {}
        @pytest.mark.parametrize("g", ["a", "b", "c", "d"])
        @pytest.mark.flaky(reruns=2)
        def test_flaky(g):
            COUNT[g] = COUNT.get(g, 0) + 1
            assert COUNT[g] >= 2
    """)
    rows = {}
    for name, mode in MODES.items():
        result, rows[name] = report_log(pytester, *mode)
        outcomes = result.parseoutcomes()
        assert (outcomes.get("passed"), outcomes.get("rerun")) == (4, 4), (name, outcomes)
    assert rows["lanes"] == rows["xdist"] == rows["hybrid"]


def test_module_fixture_setups_match_xdist_under_loadfile(pytester):
    pytester.makeconftest("""
        import pytest
        @pytest.fixture(scope="module")
        def mod(request):
            (request.config.rootpath / f"setup-{request.module.__name__}-{id(object())}").touch()
            yield
    """)
    for m in ("m1", "m2"):
        pytester.makepyfile(**{f"test_{m}": "import pytest\n@pytest.mark.parametrize('i', range(4))\n"
                                             "def test_t(i, mod): pass\n"})
    for name, mode in MODES.items():
        for f in pytester.path.glob("setup-*"):
            f.unlink()
        run(pytester, *dist_args(mode, "loadfile")).assert_outcomes(passed=8)
        setups = sorted(f.name.split("-")[1] for f in pytester.path.glob("setup-*"))
        assert setups == ["test_m1", "test_m2"], (name, setups)


def test_rerunfailures_under_concurrent_lanes_in_a_worker(pytester):
    # pytest-rerunfailures >= 15 gives each xdist worker one socket to the controller's
    # rerun database. A worker's lanes share it; unsynchronised, their request/response
    # pairs interleaved and a lane died with ValueError (INTERNALERROR).
    pytest.importorskip("pytest_rerunfailures")
    pytester.makepyfile("""
        import pytest
        COUNT = {}
        @pytest.mark.parametrize("i", range(64))
        @pytest.mark.flaky(reruns=2)
        def test_flaky(i):
            COUNT[i] = COUNT.get(i, 0) + 1
            assert COUNT[i] >= 2
    """)
    r = run(pytester, "-n", "2", "--lanes", "8", timeout=120)
    assert "INTERNALERROR" not in r.stdout.str()
    outcomes = r.parseoutcomes()
    assert (outcomes.get("passed"), outcomes.get("rerun")) == (64, 64), outcomes


@pytest.mark.parametrize("name", ["lanes", "hybrid"])
def test_group_suffix_follows_dist_as_in_xdist(pytester, name):
    # xdist's worker adds the @group suffix when --dist is loadgroup, whatever scheduler a
    # conftest returns; single-process lanes decided by the scheduler's class instead.
    pytester.makeconftest(ENV_SCHED)
    pytester.makepyfile("""
        import pytest
        @pytest.mark.xdist_group("g1")
        @pytest.mark.parametrize("env", ["envA", "envB"])
        def test_t(env):
            pass
    """)
    xdist = report_log(pytester, *dist_args(MODES["xdist"], "loadgroup"))[1]
    lanes = report_log(pytester, *dist_args(MODES[name], "loadgroup"))[1]
    assert lanes == xdist
    assert all(row[0].endswith("@g1") for row in xdist), xdist


# ---------------------------------------------------------------- rerunfailures
RERUN_ENV_CONFTEST = """
import json, os, threading, pytest
LOG = os.path.join(os.path.dirname(__file__), "ev.log")
L = threading.Lock()
@pytest.fixture(scope="module")
def env(request, worker_id):
    name = request.module.__name__
    with L, open(LOG, "a") as f: f.write(json.dumps(["setup", name, worker_id]) + "\\n")
    yield name
    with L, open(LOG, "a") as f: f.write(json.dumps(["teardown", name, worker_id]) + "\\n")
"""


@pytest.mark.parametrize("name", ["lanes", "hybrid"])
def test_rerun_does_not_tear_down_another_lanes_module_fixture(pytester, name):
    # pytest-rerunfailures (>= 16) moves the setup stack above a test it will rerun into
    # one module-level dict, and every test's teardown moves that dict into its own
    # SetupState: another lane's test took the rerunning lane's entries, so a module's
    # environment was torn down under running tests, or never (run still green).
    rf = pytest.importorskip("pytest_rerunfailures")
    if not hasattr(rf, "suspended_finalizers"):
        pytest.skip("this pytest-rerunfailures has no suspended_finalizers")
    pytester.makeconftest(RERUN_ENV_CONFTEST)
    pytester.makepyfile(test_flaky="""
        import time
        def test_f1(env, request):
            time.sleep(0.2)
            assert request.node.execution_count >= 2
        def test_f2(env): time.sleep(0.3)
    """)
    for mod, n in (("test_ok_a", 10), ("test_ok_b", 20), ("test_ok_c", 40)):
        pytester.makepyfile(**{mod: f"""
            import time, pytest
            @pytest.mark.parametrize("i", range({n}))
            def test_ok(env, i): time.sleep(0.1)
        """})
    r = run(pytester, *dist_args(MODES[name], "loadscope"), "--reruns", "1", "--reruns-delay", "1", timeout=120)
    outcomes = r.parseoutcomes()
    assert (outcomes.get("passed"), outcomes.get("rerun")) == (72, 1), r.stdout.str()[-2000:]
    import collections
    events = [json.loads(line) for line in open(pytester.path / "ev.log")]
    count = collections.Counter((kind, mod, wid) for kind, mod, wid in events)
    for (kind, mod, wid), n in count.items():
        assert n == 1 and count[("teardown" if kind == "setup" else "setup", mod, wid)] == 1, (events, count)


def test_rerun_suite_counter_is_serialized_across_lanes(pytester):
    # Two of the five socket methods of rerunfailures' per-worker client were left
    # unserialized: concurrent lanes interleaved on its socket (INTERNALERROR).
    rf = pytest.importorskip("pytest_rerunfailures")
    if not hasattr(getattr(rf, "ClientStatusDB", None), "try_increment_suite_reruns"):
        pytest.skip("this pytest-rerunfailures has no suite rerun counter")
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("i", range(200))
        def test_x(i, request):
            assert request.node.execution_count >= 2
    """)
    r = run(pytester, "-n", "2", "--lanes", "8", "--reruns", "1", "--max-suite-reruns", "1000", timeout=180)
    outcomes = r.parseoutcomes()
    assert (outcomes.get("passed"), outcomes.get("rerun")) == (200, 200), r.stdout.str()[-2000:]


def test_collection_error_output_is_captured(pytester):
    # Lanes turn pytest's capture off, so a module printing before its import failed
    # wrote to the terminal (or, in a hybrid worker, nowhere) instead of the report.
    pytester.makepyfile(test_col="print('PRINTED-AT-IMPORT')\nraise RuntimeError('boom at import')\n",
                        test_ok="def test_ok(): pass\n")
    sections = {}
    for name, mode in MODES.items():
        _, _ = report_log(pytester, *mode, timeout=60)
        for e in map(json.loads, open(pytester.path / "rl.jsonl")):
            if e.get("$report_type") == "CollectReport" and e["outcome"] == "failed":
                sections[name] = [(t, "PRINTED-AT-IMPORT" in text) for t, text in e["sections"]]
    assert sections["lanes"] == sections["xdist"] and ("Captured stdout", True) in sections["xdist"], sections
    assert sections["hybrid"] == sections["xdist"], sections


def test_group_suffix_with_two_group_marks(pytester):
    # The suffix followed xdist 3.8's rule (every mark, sorted) whatever xdist was installed;
    # xdist 3.6 uses the closest mark only.
    pytester.makepyfile(test_g="""
        import pytest
        @pytest.mark.xdist_group("outer")
        class TestC:
            @pytest.mark.xdist_group("inner")
            def test_two_groups(self):
                pass
    """)
    _, xdist_rows = report_log(pytester, "-n", "2", "--dist", "loadgroup", timeout=60)
    _, lanes_rows = report_log(pytester, "--lanes", "2", "--lanes-dist", "loadgroup", timeout=60)
    assert lanes_rows == xdist_rows

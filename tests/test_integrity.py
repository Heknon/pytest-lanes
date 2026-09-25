"""Silent corruption must be loud.

A race that crashes is found. The dangerous one leaves a run green while a report,
an output or a fixture value belonged to another test. Two defences, tested here:

* the run-time integrity check (integrity.py), active in every run: it fails the
  run with an INTERNALERROR when a lane's report stream does not match what the
  lane was running;
* a canary suite run under maximum thread-switching pressure: every test checks
  that what it sees and what is reported for it is its own, and the report-log
  must equal plain xdist's.
"""
import json

import pytest
from lanes_testing import MODES, report_log, run

LANE_MODES = {"lanes": MODES["lanes"], "hybrid": MODES["hybrid"]}


# ---------------------------------------------------------------- run-time integrity check
FOREIGN_REPORT = """
from _pytest.reports import TestReport
def test_a():
    pass
def test_b(request):
    # Stands in for a race that attributes a report to a test the lane is not running.
    report = TestReport(nodeid="test_x.py::test_a", location=("test_x.py", 0, "test_a"),
                        keywords={}, outcome="passed", longrepr=None, when="call")
    request.config.hook.pytest_runtest_logreport(report=report)
"""

SILENT_PROTOCOL = """
import pytest
from _pytest.runner import runtestprotocol
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    # Stands in for a race that loses an item's reports: it runs, nothing is logged.
    if item.name == "test_b":
        runtestprotocol(item, nextitem=nextitem, log=False)
        return True
"""


def assert_integrity_failure(result, *fragments):
    out = result.stdout.str() + result.stderr.str()
    assert result.ret != 0, out
    assert "pytest-threadlanes integrity check failed" in out, out
    for fragment in fragments:
        assert fragment in out, out


@pytest.mark.parametrize("name", LANE_MODES)
def test_report_for_an_item_the_lane_is_not_running_fails_the_run(pytester, name):
    pytester.makepyfile(test_x=FOREIGN_REPORT)
    r = run(pytester, *LANE_MODES[name], timeout=60)
    assert_integrity_failure(r, "test_x.py::test_a", "test_x.py::test_b")


@pytest.mark.parametrize("name", LANE_MODES)
def test_item_finished_without_its_reports_fails_the_run(pytester, name):
    pytester.makeconftest(SILENT_PROTOCOL)
    pytester.makepyfile(test_x="def test_a(): pass\ndef test_b(): pass\n")
    r = run(pytester, *LANE_MODES[name], timeout=60)
    assert_integrity_failure(r, "test_x.py::test_b")


@pytest.mark.parametrize("name", LANE_MODES)
def test_every_outcome_passes_the_integrity_check(pytester, name):
    # No false alarms: skips, xfails, setup/teardown errors, reruns, exclusive tests
    # and a -x stop all produce well-formed streams.
    pytester.makepyfile(test_x="""
        import pytest
        @pytest.fixture
        def bad_setup(): raise RuntimeError("setup")
        @pytest.fixture
        def bad_teardown():
            yield
            raise RuntimeError("teardown")
        def test_pass(): pass
        def test_fail(): assert 0
        def test_skip(): pytest.skip("s")
        @pytest.mark.xfail
        def test_xfail(): assert 0
        def test_setup_error(bad_setup): pass
        def test_teardown_error(bad_teardown): pass
        @pytest.mark.lanes_exclusive
        def test_exclusive(): pass
        def test_capsys(capsys): print("x")
        @pytest.mark.parametrize("i", range(20))
        def test_many(i): pass
    """)
    r = run(pytester, *LANE_MODES[name], timeout=60)
    assert "integrity check failed" not in r.stdout.str() + r.stderr.str()
    r.assert_outcomes(passed=24, failed=1, skipped=1, xfailed=1, errors=2)


@pytest.mark.parametrize("name", LANE_MODES)
def test_reruns_and_stop_pass_the_integrity_check(pytester, name):
    pytest.importorskip("pytest_rerunfailures")
    pytester.makepyfile(test_x="""
        import pytest
        @pytest.mark.flaky(reruns=2)
        def test_flaky(): assert 0
        @pytest.mark.parametrize("i", range(10))
        def test_many(i): pass
    """)
    r = run(pytester, *LANE_MODES[name], "-x", timeout=60)
    assert "integrity check failed" not in r.stdout.str() + r.stderr.str()
    assert r.ret == 2        # Interrupted, as under xdist's DSession


# ---------------------------------------------------------------- canary suite under pressure
CANARY_CONFTEST = """
import itertools, sys, threading, uuid, pytest
sys.setswitchinterval(1e-6)          # switch threads as often as the interpreter allows
_serial = itertools.count()

@pytest.fixture(scope="session")
def session_fx(worker_id):
    return worker_id, uuid.uuid4().hex
@pytest.fixture(scope="module")
def module_fx(worker_id):
    return worker_id, uuid.uuid4().hex
@pytest.fixture(scope="class")
def class_fx(worker_id):
    return worker_id, uuid.uuid4().hex
@pytest.fixture
def function_fx(request):
    return request.node.nodeid, next(_serial)
"""

CANARY_TESTS = """
import logging, sys, threading, time, pytest
log = logging.getLogger("canary")

def check(tok, worker_id, request, tmp_path, caplog, session_fx, module_fx, function_fx):
    assert session_fx[0] == worker_id and module_fx[0] == worker_id, (session_fx, module_fx, worker_id)
    assert function_fx[0] == request.node.nodeid, function_fx
    assert request.config.workerinput["workerid"] == worker_id
    assert list(tmp_path.iterdir()) == []
    (tmp_path / "mine").write_text(tok)
    for k in range(30):
        print(tok)
        sys.stderr.write(tok + "\\n")
        log.warning(tok)
        if k % 10 == 0:
            time.sleep(0)
    assert (tmp_path / "mine").read_text() == tok
    assert [r.getMessage() for r in caplog.records] == [tok] * 30
    assert [p.name for p in tmp_path.iterdir()] == ["mine"]

@pytest.mark.parametrize("i", range(150))
def test_fn(i, worker_id, request, tmp_path, caplog, session_fx, module_fx, function_fx):
    check(f"TOK-fn-{i}", worker_id, request, tmp_path, caplog, session_fx, module_fx, function_fx)

class TestCls:
    @pytest.mark.parametrize("i", range(50))
    def test_m(self, i, worker_id, request, tmp_path, caplog, session_fx, module_fx, class_fx, function_fx):
        assert class_fx[0] == worker_id
        check(f"TOK-m-{i}", worker_id, request, tmp_path, caplog, session_fx, module_fx, function_fx)
"""

CANARY_MODES = {
    "xdist": ["-n", "2"],
    "lanes": ["--lanes", "48"],
    "hybrid": ["-n", "2", "--lanes", "24"],
}


def token_of(nodeid: str) -> str:
    i = nodeid.rsplit("[", 1)[1].rstrip("]")
    return f"TOK-{'m' if '::TestCls::' in nodeid else 'fn'}-{i}"


@pytest.mark.parametrize("name", ["lanes", "hybrid"])
def test_canary_suite_under_switching_pressure(pytester, name):
    pytester.makeconftest(CANARY_CONFTEST)
    pytester.makepyfile(test_canary=CANARY_TESTS)
    result, rows = report_log(pytester, *CANARY_MODES[name], "-rA", timeout=300)
    result.assert_outcomes(passed=200)

    # Every section of every report holds this test's token and nothing else.
    seen = 0
    for event in map(json.loads, open(pytester.path / "rl.jsonl")):
        if event.get("$report_type") != "TestReport":
            continue
        tok = token_of(event["nodeid"])
        for title, text in event["sections"]:
            lines = [ln for ln in text.splitlines() if "TOK-" in ln]
            foreign = [ln for ln in lines if not ln.endswith(tok)]
            assert not foreign, (event["nodeid"], event["when"], title, foreign[:3])
            if event["when"] == "call":
                assert len(lines) == 30, (event["nodeid"], title, len(lines))
                seen += 1
    assert seen == 200 * 3, seen                 # stdout, stderr and log sections

    _, xdist_rows = report_log(pytester, *CANARY_MODES["xdist"], timeout=300)
    assert rows == xdist_rows


# ---------------------------------------------------------------- stdio replaced while lanes run (round 4)
SWAPPER = """
import io, sys, time, pytest
{mark}
def test_swap():
    real = sys.stdout
    sys.stdout = io.StringIO()        # what click's CliRunner does, for the whole process
    try:
        time.sleep(0.3)
    finally:
        sys.stdout = real
@pytest.mark.parametrize("i", range(3))
def test_printer(i):
    for _ in range(60):
        print(f"other-{{i}}"); time.sleep(0.005)
"""


@pytest.mark.parametrize("name", LANE_MODES)
def test_replacing_sys_stdout_while_lanes_run_fails_the_run(pytester, name):
    # Other lanes' output went into the replacement, silently. Now the run fails and says why.
    pytester.makepyfile(test_x=SWAPPER.format(mark=""))
    r = run(pytester, *LANE_MODES[name], timeout=60)
    assert_integrity_failure(r, "sys.stdout was replaced", "lanes_exclusive")


@pytest.mark.parametrize("name", LANE_MODES)
def test_replacing_sys_stdout_in_an_exclusive_test_is_fine(pytester, name):
    pytester.makepyfile(test_x=SWAPPER.format(mark="@pytest.mark.lanes_exclusive"))
    r = run(pytester, *LANE_MODES[name], timeout=60)
    assert "integrity check failed" not in r.stdout.str() + r.stderr.str()
    r.assert_outcomes(passed=4)


def test_capsys_and_no_capture_raise_no_stdio_alarm(pytester):
    pytester.makepyfile(test_x="""
        import time, pytest
        def test_capsys(capsys):
            print("x"); assert capsys.readouterr().out == "x\\n"
        @pytest.mark.parametrize("i", range(4))
        def test_t(i): time.sleep(0.1)
    """)
    for extra in ([], ["-s"]):
        r = run(pytester, "--lanes", "3", *extra, timeout=60)
        assert "integrity check failed" not in r.stdout.str() + r.stderr.str(), extra
        r.assert_outcomes(passed=5)



def test_lanes_waiting_for_an_exclusive_test_are_not_running(pytester):
    # A lane queued behind an exclusive test counted as running a test, so a lone test
    # replacing sys.stdout failed the run although nothing ran beside it (round-5 review).
    pytester.makepyfile(test_x="""
        import io, sys, time, pytest
        def test_a_swap():
            real = sys.stdout
            sys.stdout = io.StringIO()
            try:
                time.sleep(0.5)
            finally:
                sys.stdout = real
        @pytest.mark.lanes_exclusive
        def test_b_excl():
            pass
    """)
    r = run(pytester, "-n", "1", "--lanes", "2", timeout=60)
    assert "integrity check failed" not in r.stdout.str() + r.stderr.str(), r.stdout.str()
    r.assert_outcomes(passed=2)


def test_stdio_message_is_accurate_with_no_capture(pytester):
    # With -s, redirect_stdout is process-wide (lanes capture nothing to redirect per
    # lane): the message said it was per lane.
    pytester.makepyfile(test_x="""
        import contextlib, io, time, pytest
        @pytest.mark.parametrize("i", range(3))
        def test_t(i):
            with contextlib.redirect_stdout(io.StringIO()):
                time.sleep(0.3)
    """)
    r = run(pytester, "--lanes", "3", "-s", timeout=60)
    assert_integrity_failure(r, "sys.stdout was replaced", "with -s")

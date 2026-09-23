"""Isolation: each test's output, logs and per-worker resources stay its own.

Where a test needs other lanes to be running at the same time, it uses a
threading.Barrier across lanes rather than sleeps, so concurrency is guaranteed.
Those tests run lanes in one process (ONE_PROCESS); plain xdist has no shared
process, so there the barrier is off.
"""
import sys

import pytest
from lanes_testing import MODES, report_log, run

ONE_PROCESS = {"lanes": ["--lanes", "3"], "hybrid": ["-n", "1", "--lanes", "3"]}

#: conftest helper: wait until LANES_BARRIER tests of this process are all inside a test.
BARRIER = """
import os, threading
_B = threading.Barrier(int(os.environ.get("LANES_BARRIER") or 1), timeout=30)
def together():
    if os.environ.get("LANES_BARRIER"):
        _B.wait()
"""


# ---------------------------------------------------------------- stdout / stderr
@pytest.mark.parametrize("flag", ["-s", "--capture=no"])
def test_no_capture_flag_has_parity(pytester, flag):
    # With -s, xdist does not capture test output; lanes must not either.
    pytester.makepyfile("""
        import sys, logging
        def test_t():
            print("out"); sys.stderr.write("err\\n"); logging.getLogger("x").warning("log")
    """)
    rows = {name: report_log(pytester, *mode, flag)[1] for name, mode in MODES.items()}
    assert rows["lanes"] == rows["xdist"]
    assert rows["hybrid"] == rows["xdist"]


def test_writes_through_stdout_buffer_are_captured_per_test(pytester):
    pytester.makepyfile("""
        import sys, pytest
        @pytest.mark.parametrize("i", range(3))
        def test_b(i):
            sys.stdout.buffer.write(f"raw {i}\\n".encode()); sys.stdout.flush()
            print("text", i)
    """)
    rows = {}
    for name, mode in MODES.items():
        result, rows[name] = report_log(pytester, *mode, "-rP")
        for i in range(3):                                   # in the report, not lost
            result.stdout.fnmatch_lines([f"raw {i}"])
    assert rows["lanes"] == rows["xdist"]
    assert rows["hybrid"] == rows["xdist"]


@pytest.mark.parametrize("name", MODES.keys())
def test_doctests_do_not_steal_other_lanes_output(pytester, name):
    # doctest swaps sys.stdout for the whole process while it runs; under lanes that
    # captured every other lane's prints and failed the doctest. The printing tests
    # keep printing for a while so that a concurrently running doctest overlaps them.
    pytester.makepyfile(test_a="""
        import time, pytest
        def documented():
            '''
            >>> import time; time.sleep(0.3)
            >>> print("doc")
            doc
            '''
        @pytest.mark.parametrize("i", range(2))
        def test_p(i):
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                print("mine", i); time.sleep(0.01)
    """)
    rows = report_log(pytester, *MODES[name], "--doctest-modules", timeout=90)[1]
    assert ("test_a.py::test_a.documented", "call", "passed", ()) in rows
    for i in range(2):
        assert (f"test_a.py::test_p[{i}]", "call", "passed", ("Captured stdout call",)) in rows


@pytest.mark.parametrize("fixture", ["capsys", "capfd"])
def test_capture_fixture_requested_dynamically_fails_closed(pytester, fixture):
    # A capture fixture swaps process-wide streams, so its test must run exclusively.
    # Requested through getfixturevalue it is invisible at collection time; the test
    # must fail with instructions rather than capture other lanes' output.
    pytester.makepyfile(f"""
        def test_dyn(request):
            request.getfixturevalue("{fixture}")
    """)
    r = run(pytester, "--lanes", "2", timeout=60)
    r.assert_outcomes(failed=1)          # requested during the call, so the call fails
    r.stdout.fnmatch_lines([f"*'{fixture}' was requested at run time*lanes_exclusive*"])


def test_capture_fixture_requested_dynamically_works_when_marked_exclusive(pytester):
    pytester.makepyfile("""
        import pytest
        @pytest.mark.lanes_exclusive
        def test_dyn(request):
            cap = request.getfixturevalue("capsys")
            print("mine")
            assert cap.readouterr().out == "mine\\n"
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=1)


# ---------------------------------------------------------------- logging
def test_loggers_created_while_other_lanes_start_tests(pytester):
    # pytest >= 9 iterates logging's loggerDict when a test phase starts; a logger
    # created concurrently on another lane made that raise "dictionary changed size".
    pytester.makepyfile("""
        import logging, uuid, pytest
        @pytest.mark.parametrize("i", range(1500))
        def test_t(i):
            for _ in range(40):
                logging.getLogger(f"dyn.{uuid.uuid4().hex}")
    """)
    r = run(pytester, "--lanes", "64", timeout=300)
    r.assert_outcomes(passed=1500)


# ---------------------------------------------------------------- per-worker resources
@pytest.mark.parametrize("name", MODES.keys())
def test_session_fixture_can_use_fixed_name_temp_dir(pytester, name):
    # xdist gives every worker its own basetemp (popen-gwN), so a per-worker session
    # fixture may mktemp a fixed name. Lanes are workers: they need the same.
    pytester.makeconftest("""
        import pytest
        @pytest.fixture(scope="session")
        def db(tmp_path_factory):
            path = tmp_path_factory.mktemp("db", numbered=False)
            (path / "data").write_text("x")
            yield path
    """)
    pytester.makepyfile("""
        import time, pytest
        @pytest.mark.parametrize("i", range(6))
        def test_db(i, db, tmp_path_factory):
            assert db.parent == tmp_path_factory.getbasetemp()
            time.sleep(0.05)
    """)
    run(pytester, *MODES[name], timeout=60).assert_outcomes(passed=6)


@pytest.mark.parametrize("name", MODES.keys())
def test_basetemp_parent_is_shared_by_all_workers(pytester, monkeypatch, name):
    # xdist documents getbasetemp().parent as the directory shared by every worker
    # of a run (for cross-worker files and locks). It must stay so for lanes.
    monkeypatch.setenv("LANES_OUT", str(pytester.path))
    pytester.makepyfile("""
        import os, pathlib, time, pytest
        @pytest.mark.parametrize("i", range(6))
        def test_t(i, tmp_path_factory):
            time.sleep(0.05)
            out = pathlib.Path(os.environ["LANES_OUT"])
            (out / f"parent-{i}").write_text(str(tmp_path_factory.getbasetemp().parent))
    """)
    run(pytester, *MODES[name], f"--basetemp={pytester.path / 'bt'}", timeout=60).assert_outcomes(passed=6)
    parents = {(pytester.path / f"parent-{i}").read_text() for i in range(6)}
    assert parents == {str(pytester.path / "bt")}, parents


@pytest.mark.parametrize("mode", ONE_PROCESS.values(), ids=ONE_PROCESS.keys())
def test_each_lane_has_its_own_basetemp(pytester, monkeypatch, mode):
    pytester.makeconftest(BARRIER)
    monkeypatch.setenv("LANES_BARRIER", "3")
    monkeypatch.setenv("LANES_OUT", str(pytester.path))
    pytester.makepyfile("""
        import os, pathlib, pytest
        from conftest import together
        @pytest.mark.parametrize("i", range(3))
        def test_t(i, tmp_path_factory, tmp_path):
            together()
            base = tmp_path_factory.getbasetemp()
            assert tmp_path.parent == base
            (pathlib.Path(os.environ["LANES_OUT"]) / f"base-{i}").write_text(str(base))
    """)
    run(pytester, *mode, f"--basetemp={pytester.path / 'bt'}", timeout=60).assert_outcomes(passed=3)
    bases = {(pytester.path / f"base-{i}").read_text() for i in range(3)}
    assert len(bases) == 3, bases


IDENTITY_TESTS = """
import json, os, pathlib, pytest, xdist
from conftest import together
@pytest.mark.parametrize("i", range(3))
def test_id(i, request, worker_id, testrun_uid):
    together()
    ident = {"worker_id": worker_id, "api": xdist.get_xdist_worker_id(request),
             "is_worker": xdist.is_xdist_worker(request),
             "workerinput": request.config.workerinput["workerid"],
             "count": request.config.workerinput["workercount"], "uid": testrun_uid}
    (pathlib.Path(os.environ["LANES_OUT"]) / f"id-{i}.json").write_text(json.dumps(ident))
"""
IDENTITY_CONFTEST = BARRIER + """
def pytest_sessionfinish(session):
    if os.environ.get("LANES_MAIN_ROLE"):     # the main thread keeps the controller role
        (__import__("pathlib").Path(os.environ["LANES_OUT"]) / "main-role").write_text(
            str(hasattr(session.config, "workerinput")))
"""


@pytest.mark.parametrize("mode,expected", [
    (["--lanes", "3"], {"ln0", "ln1", "ln2"}),
    (["-n", "1", "--lanes", "3"], {"gw0.ln0", "gw0.ln1", "gw0.ln2"}),
], ids=["lanes", "hybrid"])
def test_each_lane_is_its_own_xdist_worker(pytester, monkeypatch, mode, expected):
    # Suites name per-worker resources (databases, ports, dirs) after worker_id.
    # Concurrent lanes must therefore see distinct ids, through every xdist API.
    import json
    pytester.makeconftest(IDENTITY_CONFTEST)
    pytester.makepyfile(IDENTITY_TESTS)
    monkeypatch.setenv("LANES_BARRIER", "3")
    monkeypatch.setenv("LANES_OUT", str(pytester.path))
    monkeypatch.setenv("LANES_MAIN_ROLE", "1" if mode[0] == "--lanes" else "")
    run(pytester, *mode, timeout=60).assert_outcomes(passed=3)
    idents = [json.loads((pytester.path / f"id-{i}.json").read_text()) for i in range(3)]
    assert {d["worker_id"] for d in idents} == expected, idents
    for d in idents:
        assert d["worker_id"] == d["api"] == d["workerinput"] and d["is_worker"], d
        assert d["count"] == 3, d
    assert len({d["uid"] for d in idents}) == 1, idents        # one test run
    if mode[0] == "--lanes":
        assert (pytester.path / "main-role").read_text() == "False"


# ---------------------------------------------------------------- warnings before 3.14
WARNS_TESTS = """
import warnings, pytest
def test_plain():
    with pytest.warns(UserWarning):
        warnings.warn("w", UserWarning)
def test_deprecated_call():
    with pytest.deprecated_call():
        warnings.warn("d", DeprecationWarning)
@pytest.mark.lanes_exclusive
def test_marked():
    with pytest.warns(UserWarning):
        warnings.warn("w", UserWarning)
"""


@pytest.mark.parametrize("mode", [["--lanes", "2"], ["-n", "1", "--lanes", "2"]], ids=["lanes", "hybrid"])
def test_pytest_warns_fails_closed_without_context_aware_warnings(pytester, monkeypatch, mode):
    # Before 3.14 (or with context-aware warnings off) catch_warnings swaps process-wide
    # state: concurrent pytest.warns blocks failed 5 times in 6. A non-exclusive test
    # using it must fail deterministically, with instructions, instead of flaking.
    monkeypatch.setenv("PYTHON_CONTEXT_AWARE_WARNINGS", "0")   # ignored before 3.14
    pytester.makepyfile(WARNS_TESTS)
    r = run(pytester, *mode, "-rf", timeout=60)
    r.assert_outcomes(passed=1, failed=2)
    assert r.stdout.str().count("mark it @pytest.mark.lanes_exclusive") == 2, r.stdout.str()


def test_pytest_warns_runs_normally_with_context_aware_warnings(pytester, monkeypatch):
    if sys.version_info < (3, 14):
        pytest.skip("needs context-aware warnings (Python 3.14+)")
    monkeypatch.setenv("PYTHON_CONTEXT_AWARE_WARNINGS", "1")
    pytester.makepyfile(WARNS_TESTS)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=3)

"""Isolation: each test's output, logs and per-worker resources stay its own.

Where a test needs other lanes to be running at the same time, it uses a
threading.Barrier across lanes rather than sleeps, so concurrency is guaranteed.
Those tests run lanes in one process (ONE_PROCESS); plain xdist has no shared
process, so there the barrier is off.
"""
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
        result.stdout.fnmatch_lines(["*raw 0*", "*raw 1*", "*raw 2*"])   # in the report, not lost
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
    r.assert_outcomes(errors=1)
    r.stdout.fnmatch_lines(["*lanes_exclusive*"])


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

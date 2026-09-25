"""Isolation: each test's output, logs and per-worker resources stay its own.

Where a test needs other lanes to be running at the same time, it uses a
threading.Barrier across lanes rather than sleeps, so concurrency is guaranteed.
Those tests run lanes in one process (ONE_PROCESS); plain xdist has no shared
process, so there the barrier is off.
"""
import sys

import pytest
from lanes_testing import CONTEXT_AWARE_WARNINGS, MODES, report_log, run, without

#: BASE with the cache provider on (it is off everywhere else). BASE is "-p X" pairs.
WITH_CACHE = without("cacheprovider")

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


# ---------------------------------------------------------------- stdin
@pytest.mark.parametrize("name", MODES.keys())
def test_reading_stdin_fails_as_under_capture(pytester, name):
    # pytest's capture replaces sys.stdin so a test reading it fails at once. Lanes turn
    # pytest's capture off, so without the same guard a lane blocked on the terminal
    # forever (found with a pty); without a terminal it raised EOFError instead.
    pytester.makepyfile("""
        def test_input():
            input("prompt> ")
        def test_other():
            pass
    """)
    r = run(pytester, *MODES[name], timeout=60)
    r.assert_outcomes(passed=1, failed=1)
    r.stdout.fnmatch_lines(["*OSError: pytest: reading from stdin while output is captured*"])


def test_stdin_is_left_alone_with_no_capture(pytester):
    pytester.makepyfile("""
        import sys
        def test_input():
            assert "reading from stdin" not in type(sys.stdin).__name__
            assert sys.stdin.readline() == ""       # pytester closes stdin: EOF
    """)
    run(pytester, "--lanes", "2", "-s", timeout=60).assert_outcomes(passed=1)


# ---------------------------------------------------------------- config.cache
@pytest.mark.parametrize("mode", [["--lanes", "8"], ["-n", "1", "--lanes", "8"]], ids=["lanes", "hybrid"])
def test_config_cache_is_safe_across_lanes(pytester, mode):
    # pytest writes a cache value by truncating the file, then writing it: a lane reading
    # concurrently saw an empty file and got the default (2 of 40 tests failed).
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("i", range(200))
        def test_t(i, request):
            cache = request.config.cache
            cache.set(f"lanes/k{i % 3}", {"i": i, "pad": "x" * 20000})
            value = cache.get(f"lanes/k{i % 3}", None)
            assert value is not None and "i" in value
    """)
    r = pytester.runpytest_subprocess(*WITH_CACHE, *mode, timeout=120)
    r.assert_outcomes(passed=200)


# ---------------------------------------------------------------- redirected and replaced stdio
REDIRECT_TESTS = """
import contextlib, io, sys, time, pytest
@pytest.mark.parametrize("i", range(4))
def test_redirect(i):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        for _ in range(20):
            print(f"mine-{i}"); sys.stderr.write(f"err-{i}\\n"); time.sleep(0.005)
        with contextlib.redirect_stdout(io.StringIO()) as inner:
            print("nested")
        assert inner.getvalue() == "nested\\n"
    assert set(out.getvalue().split()) == {f"mine-{i}"}, out.getvalue()[:200]
    assert set(err.getvalue().split()) == {f"err-{i}"}
    print(f"after-{i}")
@pytest.mark.parametrize("i", range(4))
def test_printer(i):
    for _ in range(40):
        print(f"other-{i}"); time.sleep(0.003)
"""


@pytest.mark.parametrize("name", MODES.keys())
def test_redirect_stdout_is_per_lane(pytester, name):
    # contextlib.redirect_stdout swaps sys.stdout for the whole process: under lanes it
    # captured every other lane's prints too (all 4 redirecting tests failed). On a lane
    # it now redirects that lane's output only.
    pytester.makepyfile(REDIRECT_TESTS)
    result, rows = report_log(pytester, *MODES[name], "-rA", timeout=60)
    result.assert_outcomes(passed=8)
    result.stdout.fnmatch_lines(["*after-0*"])       # output after the block is captured again


# ---------------------------------------------------------------- --setup-show / --setup-only
@pytest.mark.parametrize("flag", ["--setup-show", "--setup-only"])
def test_setup_show_on_many_lanes(pytester, flag):
    # pytest's setuponly plugin sets FixtureDef.cached_param on setup and deletes it on
    # finalization; FixtureDef is shared by all lanes, so one lane deleted what another
    # was about to print (AttributeError, seen on 3.14t). It is per lane now (P2).
    pytester.makepyfile("""
        import pytest
        @pytest.fixture(params=range(4))
        def p(request):
            return request.param
        @pytest.mark.parametrize("i", range(100))
        def test_t(i, p):
            pass
    """)
    r = run(pytester, "--lanes", "16", flag, timeout=120)
    assert r.ret == pytest.ExitCode.OK, r.stdout.str()[-2000:]


# ---------------------------------------------------------------- exclusive tests and the terminal
@pytest.mark.parametrize("fixture", ["capfd", "capfdbinary"])
def test_exclusive_capfd_test_does_not_capture_the_reporters(pytester, fixture):
    # capfd redirects fd 1 for the whole process while a phase runs. The main thread
    # replayed the test's earlier reports meanwhile, so the terminal reporter's "PASSED"
    # (or any plugin's output) went into capfd: the test's readouterr() got it, and the
    # terminal lost it (seen 4 in 15 runs on 3.14t). An exclusive test's hooks are now
    # replayed once the test is done, as xdist's controller would print them.
    pytester.makeconftest("""
        import os, time
        def pytest_runtest_logstart(nodeid, location):
            time.sleep(0.3)                     # a busy main thread: the lane moves on meanwhile
        def pytest_runtest_logreport(report):   # replayed on the main thread, like reporters
            if report.when == "setup":
                os.write(1, b"REPORT-SEEN\\n")
    """)
    pytester.makepyfile(f"""
        import os, time
        def test_fd({fixture}):
            time.sleep(0.5)                     # the setup report is replayed meanwhile
            os.write(1, b"raw")
            out = {fixture}.readouterr().out
            assert out in ("raw", b"raw"), out
    """)
    r = run(pytester, "--lanes", "2", "-v", timeout=60)
    r.assert_outcomes(passed=1)
    assert "REPORT-SEEN" in r.stdout.str(), r.stdout.str()


@pytest.mark.parametrize("fixture", ["capfd", "capsys"])
@pytest.mark.parametrize("name", MODES.keys())
def test_setup_show_output_is_not_captured_by_the_test(pytester, name, fixture):
    # Plugins write to the terminal between capman.suspend_global_capture() and
    # resume_global_capture() (--setup-show does). In plain pytest suspending global
    # capture also bypasses a capfd/capsys fixture; lanes turn global capture off, so
    # the line went into the test's capture and its readouterr() failed. On a lane, the fixture capture is now suspended with it (P15).
    pytester.makepyfile(f"""
        import os, sys
        def test_it({fixture}):
            os.write(1, b"raw") if "{fixture}" == "capfd" else sys.stdout.write("raw")
            assert {fixture}.readouterr().out == "raw"
    """)
    result = run(pytester, *MODES[name], "--setup-show", timeout=60)
    result.assert_outcomes(passed=1)


def test_exclusive_test_starts_after_earlier_reports_are_replayed(pytester):
    # Hybrid mode runs exclusive tests between others. A lane released its share of the
    # exclusivity lock when its test ended, before the main thread replayed its reports;
    # the exclusive capfd test then captured that replay's output (a worker's progress
    # dot). Lanes now keep the lock until replayed.
    pytester.makeconftest("""
        import os, time
        def pytest_runtest_logreport(report):   # runs where reports are replayed
            if report.when == "call" and "test_normal" in report.nodeid:
                time.sleep(0.3)
                os.write(1, b"OTHER")
    """)
    pytester.makepyfile("""
        import os, time
        def test_normal():
            pass
        def test_exclusive(capfd):
            time.sleep(0.6)
            os.write(1, b"raw")
            assert capfd.readouterr().out == "raw"
    """)
    run(pytester, "-n", "1", "--lanes", "2", timeout=60).assert_outcomes(passed=2)



# ---------------------------------------------------------------- redirect edge cases
def test_redirect_stdout_to_none_discards(pytester):
    # redirect_stdout(None) is the stdlib idiom for "discard"; lanes wrote to None.
    pytester.makepyfile("""
        import contextlib, sys
        def test_it():
            with contextlib.redirect_stdout(None):
                print("gone")
                sys.stdout.write("gone")
                sys.stdout.flush()
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=1)


@pytest.mark.parametrize("name", MODES.keys())
def test_sys_stdout_inside_a_redirect_is_the_target(pytester, name):
    # Inside a per-lane redirect sys.stdout stayed the lane stream: fileno() raised and
    # getvalue() was missing, so subprocess(stdout=sys.stdout) and similar code broke.
    pytester.makepyfile("""
        import contextlib, io, subprocess, sys
        def test_file(tmp_path):
            path = tmp_path / "out.txt"
            with open(path, "w") as f, contextlib.redirect_stdout(f):
                sys.stdout.flush()
                subprocess.run([sys.executable, "-c", "print('child')"], stdout=sys.stdout, check=True)
            assert path.read_text() == "child\\n"
        def test_stringio():
            with contextlib.redirect_stdout(io.StringIO()):
                print("x")
                assert sys.stdout.getvalue() == "x\\n"
    """)
    run(pytester, *MODES[name], timeout=60).assert_outcomes(passed=2)


@pytest.mark.parametrize("name", MODES.keys())
def test_sys_stdout_behaves_like_a_real_stream(pytester, name):
    # io.TextIOBase defines encoding/errors/closed/close/seek/... so they never reached the
    # redirect target or the real stream: under lanes sys.stdout.encoding was None, seek()
    # inside redirect_stdout(StringIO()) raised, and close() there closed stdout for every
    # lane.
    pytester.makepyfile("""
        import contextlib, io, sys, time
        def test_attrs():
            s = sys.stdout
            assert s.encoding and s.writable() and not s.closed
            assert s.encoding == sys.__stdout__.encoding
        def test_redirect_io():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                s = sys.stdout
                assert (s.writable(), s.seekable(), s.readable()) == (True, True, buf.readable())
                s.write("abc"); s.seek(0); s.truncate(0); print("x")
                assert s.tell() == 2
                s.writelines(["y\\n"])
            assert buf.getvalue() == "x\\ny\\n"
        def test_close_closes_the_target_only():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sys.stdout.close()
            assert buf.closed and not sys.stdout.closed
            print("still fine")
        def test_other_lane_keeps_writing():
            time.sleep(0.3)
            sys.stdout.writelines(["line\\n"])
            assert not sys.stdout.closed
    """)
    run(pytester, *MODES[name], timeout=60).assert_outcomes(passed=4)


@pytest.mark.parametrize("name", MODES.keys())
def test_sys_stdout_is_not_the_terminal_outside_a_redirect(pytester, name):
    # Outside a redirect, sys.stdout.fileno()/isatty() returned the real terminal's: fd
    # writes escaped the test's report (single process: to the terminal; hybrid: lost)
    # and colour detection turned on inside captured output.
    # Not a tty, as under pytest's capture; fd writes go to a per-lane capture file
    # (test_fd_writes_through_sys_stdout_fileno_are_captured).
    pytester.makepyfile("""
        import io, sys, pytest
        def test_it():
            assert sys.stdout.isatty() is False and sys.stderr.isatty() is False
    """)
    run(pytester, *MODES[name], timeout=60).assert_outcomes(passed=1)


# ---------------------------------------------------------------- more capture edge cases
@pytest.mark.parametrize("name", MODES.keys())
def test_fd_writes_through_sys_stdout_fileno_are_captured(pytester, name):
    # Under fd capture (pytest's default) sys.stdout.fileno() is a capture file: a child
    # process given stdout=sys.stdout, or faulthandler, writes into the test's report.
    # Lanes raised UnsupportedOperation; each lane now has its own capture file.
    import json
    pytester.makepyfile("""
        import faulthandler, subprocess, sys
        def test_sub():
            subprocess.run([sys.executable, "-c", "print('from-child')"], stdout=sys.stdout, check=True)
            assert 0
        def test_fh():
            faulthandler.enable(file=sys.stdout)
            faulthandler.disable()
    """)
    result, _ = report_log(pytester, *MODES[name], timeout=60)
    result.assert_outcomes(passed=1, failed=1)
    call = [e for e in map(json.loads, open(pytester.path / "rl.jsonl"))
            if e.get("$report_type") == "TestReport" and e["nodeid"].endswith("test_sub") and e["when"] == "call"][0]
    assert any("from-child" in text for title, text in call["sections"] if "stdout" in title), call["sections"]


NONPROPAGATING = """
import logging
EARLY = logging.getLogger("early.private"); EARLY.propagate = False
"""


@pytest.mark.parametrize("name", ["lanes", "hybrid"])
def test_non_propagating_logger_capture_matches_xdist(pytester, name):
    # pytest 9 captures non-propagating loggers that exist when each phase starts; pytest 8
    # never does. Lanes attached once, at session start: both versions diverged.
    pytester.makeconftest(NONPROPAGATING)
    pytester.makepyfile("""
        import logging
        MOD = logging.getLogger("module.private"); MOD.propagate = False
        def test_early(caplog):
            logging.getLogger("early.private").warning("early record")
            print(sorted(r.getMessage() for r in caplog.records))
        def test_module(caplog):
            MOD.warning("module record")
            print(sorted(r.getMessage() for r in caplog.records))
    """)
    xdist = report_log(pytester, *MODES["xdist"], "-rA", timeout=60)
    lanes = report_log(pytester, *MODES[name], "-rA", timeout=60)
    assert lanes[1] == xdist[1]

    def printed(result):
        return sorted(l for l in result.stdout.lines if l.startswith("[") and "record" in l)
    assert printed(lanes[0]) == printed(xdist[0])


def test_utf8_split_across_buffer_writes(pytester):
    # Bytes written to sys.stdout.buffer were decoded one write at a time, so a character
    # split across writes (a child's output streamed in chunks) came out as U+FFFD.
    import json
    pytester.makepyfile("""
        import sys
        def test_it():
            for b in "h\\u00e9llo \\u2713".encode():
                sys.stdout.buffer.write(bytes([b]))
            sys.stdout.buffer.write(b"\\n")
    """)
    result, _ = report_log(pytester, "--lanes", "2", timeout=60)
    result.assert_outcomes(passed=1)
    call = [e for e in map(json.loads, open(pytester.path / "rl.jsonl"))
            if e.get("$report_type") == "TestReport" and e["when"] == "call"][0]
    assert ["h\u00e9llo \u2713\n"] == [t for n, t in call["sections"] if "stdout" in n], call["sections"]


@pytest.mark.parametrize("mode", [["--lanes", "3"], ["-n", "1", "--lanes", "3"]], ids=["lanes", "hybrid"])
def test_warning_shown_on_one_lane_is_not_hidden_from_another(pytester, monkeypatch, mode):
    # Python remembers a warning shown with the "default" action in the emitting module's
    # __warningregistry__, and skips the filters on a repeat. Shared by lanes, one lane's
    # "default" warning hid it from another lane whose filter is "error": the error tests
    # passed. On 3.14 each lane's warning context now ends with "always",
    # so an unmatched warning is never registered.
    if not CONTEXT_AWARE_WARNINGS:
        pytest.skip("context-aware warnings need Python 3.14 (before it, warnings run with -p no:warnings)")
    monkeypatch.setenv("PYTHON_CONTEXT_AWARE_WARNINGS", "1")
    pytester.makepyfile(helper="import warnings\ndef old():\n    warnings.warn('old api', UserWarning)\n")
    pytester.makepyfile(test_e="""
        import time, pytest, helper
        @pytest.mark.parametrize("i", range(4))
        def test_user(i):
            for _ in range(100):
                helper.old(); time.sleep(0.002)
        @pytest.mark.parametrize("i", range(4))
        @pytest.mark.filterwarnings("error::UserWarning")
        def test_must_fail(i):
            time.sleep(0.05)
            helper.old()
    """)
    base = without("warnings")
    r = pytester.runpytest_subprocess(*base, *mode, timeout=120)
    r.assert_outcomes(passed=4, failed=4, warnings=r.parseoutcomes().get("warnings", 0))


# ---------------------------------------------------------------- PYTEST_CURRENT_TEST
SPAWNER = """
import subprocess, sys, pytest
@pytest.mark.parametrize("i", range(12))
def test_spawn(i):
    sys.setswitchinterval(1e-6)
    for _ in range(30):
        out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
        assert out.stdout == "ok\\n"
@pytest.mark.parametrize("i", range(3000))
def test_phase(i):          # constant phase changes on the other lanes
    pass
"""


@pytest.mark.parametrize("name", ["lanes", "hybrid"])
def test_subprocesses_spawn_while_other_lanes_change_phase(pytester, name):
    # pytest set and deleted PYTEST_CURRENT_TEST in the process environment (putenv) at
    # every phase of every lane; a child exec'ing with the parent's environment meanwhile
    # failed with "OSError: [Errno 14] Bad address", or could get a torn environment.
    pytester.makepyfile(test_x=SPAWNER)
    r = run(pytester, *({"lanes": ["--lanes", "24"], "hybrid": ["-n", "2", "--lanes", "12"]}[name]),
            timeout=300)
    r.assert_outcomes(passed=3012)


def test_current_test_var_is_per_lane_and_not_in_the_process_environment(pytester):
    pytester.makepyfile(test_x="""
        import ctypes, os, time, pytest
        getenv = ctypes.CDLL(None).getenv
        getenv.restype = ctypes.c_char_p
        INHERITED = getenv(b"PYTEST_CURRENT_TEST")     # the outer pytest's, when run from tests/
        @pytest.fixture
        def fx(request):
            assert os.environ["PYTEST_CURRENT_TEST"] == request.node.nodeid + " (setup)"
            yield
            assert os.environ["PYTEST_CURRENT_TEST"] == request.node.nodeid + " (teardown)"
        @pytest.mark.parametrize("i", range(12))
        def test_t(i, fx, request):
            mine = request.node.nodeid + " (call)"
            for _ in range(50):
                assert os.environ["PYTEST_CURRENT_TEST"] == mine
                assert os.getenv("PYTEST_CURRENT_TEST") == mine
                assert os.environ.copy()["PYTEST_CURRENT_TEST"] == mine
                assert "PYTEST_CURRENT_TEST" in os.environ and "PYTEST_CURRENT_TEST" in list(os.environ)
                assert getenv(b"PYTEST_CURRENT_TEST") == INHERITED   # the C environment is untouched
                time.sleep(0.002)
        def test_zz_after():
            pass
    """)
    r = run(pytester, "--lanes", "6", timeout=120)
    r.assert_outcomes(passed=13)


def test_changed_current_test_var_format_fails_closed(pytester):
    # Lanes compute PYTEST_CURRENT_TEST themselves (P6): if pytest's value changes, refuse.
    pytester.makeconftest("""
        import os
        from _pytest import runner
        def _update_current_test_var(item, when):
            if when:
                os.environ["PYTEST_CURRENT_TEST"] = f"{item.nodeid} [{when}]".replace("\\x00", "")
            else:
                os.environ.pop("PYTEST_CURRENT_TEST")
        runner._update_current_test_var = _update_current_test_var
    """)
    pytester.makepyfile(test_x="def test_t(): pass")
    r = run(pytester, "--lanes", "2", timeout=60)
    assert r.ret == pytest.ExitCode.USAGE_ERROR, r.stdout.str()
    assert "P6 PYTEST_CURRENT_TEST format changed" in r.stderr.str() + r.stdout.str()


def test_children_do_not_inherit_an_outer_current_test_var(pytester, monkeypatch):
    # Under an outer pytest (or any parent that set it), children started without env=
    # reported the outer test's name.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "outer")
    pytester.makepyfile(test_x="""
        import os, subprocess, sys
        def test_it():
            out = subprocess.run([sys.executable, "-c",
                                  "import os; print(os.environ.get('PYTEST_CURRENT_TEST'))"],
                                 capture_output=True, text=True).stdout.strip()
            assert out == "None", out
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=1)


@pytest.mark.parametrize("mode,count", [(["--lanes", "3"], "3"), (["-n", "2", "--lanes", "2"], "4")],
                         ids=["lanes", "hybrid"])
def test_xdist_worker_environment_variables_name_the_lane(pytester, mode, count):
    # Suites name databases and directories after PYTEST_XDIST_WORKER: shared by every
    # lane of a process, they collided silently (F11).
    pytester.makepyfile(test_x="""
        import os, time, pytest
        @pytest.mark.parametrize("i", range(8))
        def test_it(i, worker_id):
            for _ in range(20):
                assert os.environ["PYTEST_XDIST_WORKER"] == worker_id
                assert os.getenv("PYTEST_XDIST_WORKER_COUNT") == "%s"
                time.sleep(0.005)
    """ % count)
    run(pytester, *mode, timeout=60).assert_outcomes(passed=8)


# ---------------------------------------------------------------- logging edge cases
LANE_ONLY = {"lanes": MODES["lanes"], "hybrid": MODES["hybrid"]}


@pytest.mark.parametrize("name", LANE_ONLY)
def test_logger_made_to_propagate_is_captured_once(pytester, name):
    # A library logger set non-propagating, then made to propagate for caplog: records
    # reached the lane twice (through its own handler and through the root's).
    # A logger per test: `propagate` is process-wide, and the two tests run at once.
    pytester.makepyfile(test_dup="""
        import logging, pytest
        LIBS = {name: logging.getLogger("lib_" + name) for name in ("test_a", "test_b")}
        for lg in LIBS.values():
            lg.propagate = False
        @pytest.fixture
        def lib(request, caplog):
            lg = LIBS[request.node.name]
            lg.propagate = True
            yield lg, caplog
            lg.propagate = False
        def test_a(lib):
            lg, caplog = lib
            lg.warning("one")
            assert [r.getMessage() for r in caplog.records] == ["one"]
        def test_b(lib):
            lg, caplog = lib
            lg.warning("two")
            assert [r.getMessage() for r in caplog.records] == ["two"]
    """)
    run(pytester, *LANE_ONLY[name], timeout=60).assert_outcomes(passed=2)


@pytest.mark.parametrize("name", LANE_ONLY)
def test_caplog_handler_formatter_and_filters(pytester, name):
    pytester.makepyfile(test_fmt="""
        import logging
        def test_fmt(caplog):
            caplog.handler.setFormatter(logging.Formatter("CUSTOM %(message)s"))
            logging.getLogger("x").warning("hello")
            assert "CUSTOM hello" in caplog.text, repr(caplog.text)
        def test_filter(caplog):
            caplog.handler.addFilter(lambda r: "keep" in r.getMessage())
            logging.getLogger("x").warning("keep me")
            logging.getLogger("x").warning("drop me")
            assert [r.getMessage() for r in caplog.records] == ["keep me"]
    """)
    run(pytester, *LANE_ONLY[name], timeout=60).assert_outcomes(passed=2)


def test_logs_from_threads_outside_lanes_are_not_kept(pytester):
    # A background thread started at session start (not a lane) logged into the logging
    # plugin's own handlers, which nothing resets under lanes: they grew for the session.
    pytester.makeconftest("""
        import logging, threading, time
        stop = threading.Event()
        def _heartbeat():
            lg = logging.getLogger("infra.heartbeat")
            while not stop.is_set():
                lg.warning("heartbeat")
                time.sleep(0.001)
        def pytest_sessionstart(session):
            threading.Thread(target=_heartbeat, daemon=True).start()
        def pytest_sessionfinish(session):
            stop.set()
            lp = session.config.pluginmanager.get_plugin("logging-plugin")
            for name in ("caplog_handler", "report_handler"):
                h = getattr(lp, name)
                h = h.__dict__.get("_fallback", h)
                print(f"\\nKEPT {name} {len(h.records)}")
    """)
    pytester.makepyfile("""
        import time, pytest
        @pytest.mark.parametrize("i", range(2))
        def test_slow(i):
            time.sleep(0.5)
    """)
    r = run(pytester, "--lanes", "2", "-s", timeout=60)
    kept = [int(line.split()[-1]) for line in r.outlines if line.startswith("KEPT ")]
    assert kept and max(kept) < 50, kept


# ---------------------------------------------------------------- sys.stdout.buffer
def test_writes_through_the_binary_buffer_stay_in_the_lane(pytester):
    pytester.makepyfile(test_buf="""
        import os, sys
        def test_writelines():
            sys.stdout.buffer.writelines([b"VIA-WRITELINES\\n"])
            assert not sys.stdout.buffer.isatty()
            assert False
        def test_buffer_fileno():
            assert sys.stdout.buffer.fileno() == sys.stdout.fileno()
            os.write(sys.stdout.buffer.fileno(), b"VIA-FILENO\\n")
            assert False
    """)
    r = run(pytester, "--lanes", "2", "-rA", timeout=60)
    r.assert_outcomes(failed=2)
    out = r.stdout.str()
    # Each line appears only inside its test's captured section, never loose on the terminal.
    for token in ("VIA-WRITELINES", "VIA-FILENO"):
        lines = [i for i, line in enumerate(r.outlines) if line == token]
        assert lines, (token, out)
        section = max(i for i, line in enumerate(r.outlines[:lines[0]]) if "Captured stdout call" in line)
        assert section < lines[0], (token, out)


def test_single_process_lanes_have_the_worker_environment(pytester):
    pytester.makepyfile(test_w="""
        import os
        def test_it(request, testrun_uid):
            assert os.environ["PYTEST_XDIST_TESTRUNUID"] == testrun_uid
            assert "mainargv" in request.config.workerinput
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=1)

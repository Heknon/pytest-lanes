"""The patch guard: a process-wide patch in a test that is not exclusive fails at once.

Lanes share one process, so ``mock.patch`` on a module or class, ``monkeypatch.setattr``
on one, environment variables, ``chdir`` and ``sys.path`` are seen by every test
running meanwhile: they broke concurrent tests silently (round 4: 3 of 4 mocker tests
failed, others passed for the wrong reason). Like pytest.warns before 3.14 (P11), such
a test now fails with instructions, unless it is ``lanes_exclusive``, marked
``lanes_allow_patches``, or the run passes ``--lanes-allow-patches``.
"""
import pytest
from lanes_testing import MODES, run

LANE_MODES = {"lanes": MODES["lanes"], "hybrid": MODES["hybrid"]}
GUARD_MESSAGE = "*patches process-wide state*lanes_exclusive*"

UNSAFE = {
    "mock_patch_string": """
        from unittest import mock
        def test_it():
            with mock.patch("json.dumps", return_value="x"):
                pass
    """,
    "mock_patch_object_module": """
        import json
        from unittest import mock
        def test_it():
            with mock.patch.object(json, "dumps"):
                pass
    """,
    "mock_patch_object_class": """
        import json
        from unittest import mock
        def test_it():
            with mock.patch.object(json.JSONEncoder, "default"):
                pass
    """,
    "mock_patch_dict_environ": """
        import os
        from unittest import mock
        def test_it():
            with mock.patch.dict(os.environ, {"LANES_X": "1"}):
                pass
    """,
    "monkeypatch_setattr_module": """
        import json
        def test_it(monkeypatch):
            monkeypatch.setattr(json, "dumps", lambda *a, **k: "x")
    """,
    "monkeypatch_setattr_string": """
        def test_it(monkeypatch):
            monkeypatch.setattr("json.dumps", lambda *a, **k: "x")
    """,
    "monkeypatch_setenv": """
        def test_it(monkeypatch):
            monkeypatch.setenv("LANES_X", "1")
    """,
    "monkeypatch_chdir": """
        def test_it(monkeypatch, tmp_path):
            monkeypatch.chdir(tmp_path)
    """,
    "monkeypatch_syspath": """
        def test_it(monkeypatch, tmp_path):
            monkeypatch.syspath_prepend(str(tmp_path))
    """,
    "monkeypatch_setitem_environ": """
        import os
        def test_it(monkeypatch):
            monkeypatch.setitem(os.environ, "LANES_X", "1")
    """,
    "function_fixture": """
        import json, pytest
        @pytest.fixture
        def patched(monkeypatch):
            monkeypatch.setattr(json, "dumps", lambda *a, **k: "x")
        def test_it(patched):
            pass
    """,
}

SAFE = {
    "mock_patch_object_instance": """
        from unittest import mock
        class Client:
            def get(self): return "real"
        def test_it():
            c = Client()
            with mock.patch.object(c, "get", return_value="fake"):
                assert c.get() == "fake"
    """,
    "monkeypatch_setattr_instance": """
        class Client:
            def get(self): return "real"
        def test_it(monkeypatch):
            c = Client()
            monkeypatch.setattr(c, "get", lambda: "fake")
            assert c.get() == "fake"
    """,
    "monkeypatch_setitem_local_dict": """
        def test_it(monkeypatch):
            d = {}
            monkeypatch.setitem(d, "k", 1)
    """,
    "patch_dict_local": """
        from unittest import mock
        def test_it():
            d = {"a": 1}
            with mock.patch.dict(d, {"a": 2}):
                assert d["a"] == 2
    """,
    "session_fixture_env": """
        import pytest
        @pytest.fixture(scope="session", autouse=True)
        def run_env():
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("LANES_RUN", "1")
                yield
        def test_it():
            import os
            assert os.environ["LANES_RUN"] == "1"
    """,
}


@pytest.mark.parametrize("name", LANE_MODES)
@pytest.mark.parametrize("case", UNSAFE)
def test_process_wide_patch_fails_the_test(pytester, name, case):
    pytester.makepyfile(UNSAFE[case])
    r = run(pytester, *LANE_MODES[name], timeout=60)
    assert r.ret == pytest.ExitCode.TESTS_FAILED, r.stdout.str()
    r.stdout.fnmatch_lines([GUARD_MESSAGE])


@pytest.mark.parametrize("case", SAFE)
def test_test_local_patch_is_allowed(pytester, case):
    pytester.makepyfile(SAFE[case])
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=1)


@pytest.mark.parametrize("opt_out", [
    ["--lanes-allow-patches"],
    ["-o", "lanes_allow_patches=true"],
    "marker",
    "exclusive",
])
def test_opt_outs(pytester, opt_out):
    mark = {"marker": "@pytest.mark.lanes_allow_patches", "exclusive": "@pytest.mark.lanes_exclusive"}
    pytester.makepyfile(f"""
        import json, pytest
        from unittest import mock
        {mark.get(opt_out, "") if isinstance(opt_out, str) else ""}
        def test_it(monkeypatch):
            monkeypatch.setenv("LANES_X", "1")
            with mock.patch("json.dumps", return_value="x"):
                assert json.dumps(1) == "x"
    """)
    args = opt_out if isinstance(opt_out, list) else []
    run(pytester, "--lanes", "2", *args, timeout=60).assert_outcomes(passed=1)


@pytest.mark.parametrize("mode", [["-n", "2"], ["--lanes", "1"], ["-n", "2", "--lanes", "1"]],
                         ids=["xdist", "one-lane", "hybrid-one-lane"])
def test_no_guard_without_concurrent_lanes(pytester, mode):
    pytester.makepyfile(UNSAFE["mock_patch_string"])
    run(pytester, *mode, timeout=60).assert_outcomes(passed=1)


def test_mocker_is_guarded(pytester):
    pytest.importorskip("pytest_mock")
    pytester.makepyfile("""
        def test_it(mocker):
            mocker.patch("os.getcwd", return_value="/fake")
    """)
    r = run(pytester, "--lanes", "2", timeout=60)
    r.stdout.fnmatch_lines([GUARD_MESSAGE])

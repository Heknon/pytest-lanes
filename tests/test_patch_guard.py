"""The patch guard: a process-wide patch in a test that is not exclusive fails at once.

Lanes share one process, so ``mock.patch`` on a module or class, ``monkeypatch.setattr``
on one, environment variables, ``chdir`` and ``sys.path`` are seen by every test
running meanwhile: they broke concurrent tests silently. Like pytest.warns before 3.14 (P11), such
a test now fails with instructions, unless it is ``lanes_exclusive``, marked
``lanes_allow_patches``, or the run passes ``--lanes-allow-patches``. Session-scoped
fixtures are guarded too: each lane tears its own down when it finishes, undoing the
patch for lanes still running.
"""
import pytest
from lanes_testing import MODES, run

LANE_MODES = {"lanes": MODES["lanes"], "hybrid": MODES["hybrid"]}
GUARD_MESSAGE = "*patches process-wide state*"

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
    # Each lane has its own session fixture: the first lane to finish tears it down and
    # undoes the variable for the whole process while other lanes still read it (a KeyError).
    "session_fixture_env": """
        import pytest
        @pytest.fixture(scope="session", autouse=True)
        def run_env():
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("LANES_RUN", "1")
                yield
        def test_it():
            pass
    """,
    # The most common form: a settings object reached by dotted path.
    "mock_patch_dotted_instance": """
        from unittest import mock
        def test_it():
            with mock.patch("appcfg.config.timeout", 99):
                pass
    """,
    "function_fixture": """
        import json, pytest
        @pytest.fixture
        def patched(monkeypatch):
            monkeypatch.setattr(json, "dumps", lambda *a, **k: "x")
        def test_it(patched):
            pass
    """,
    # Direct writes: os.putenv/unsetenv/chdir audit events. Besides being seen by
    # every lane, an environment write while another lane starts a subprocess made that
    # spawn fail with "OSError: [Errno 14] Bad address".
    "environ_direct_write": """
        import os
        def test_it():
            os.environ["LANES_X"] = "1"
    """,
    "environ_direct_delete": """
        import os
        def test_it():
            os.environ.pop("LANES_NOT_SET", None)
            del os.environ["LANES_NOT_SET_EITHER"]
    """,
    "os_chdir_direct": """
        import os
        def test_it(tmp_path):
            os.chdir(tmp_path)
    """,
    "session_fixture_env_direct": """
        import os, pytest
        @pytest.fixture(scope="session")
        def env():
            os.environ["LANES_X"] = "1"
        def test_it(env):
            pass
    """,
}

SAFE = {
    "local_class": """
        from unittest import mock
        def test_it(monkeypatch):
            class Local:
                x = 1
            with mock.patch.object(Local, "x", 2):
                assert Local.x == 2
            monkeypatch.setattr(Local, "x", 3)
    """,
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
}


def test_session_fixture_patch_message_says_where_to_set_it_instead(pytester):
    pytester.makepyfile(UNSAFE["session_fixture_env"])
    r = run(pytester, "--lanes", "2", timeout=60)
    r.stdout.fnmatch_lines(["*session-scoped fixture*pytest_configure*"])


APPCFG = """
class Config:
    timeout = 1
config = Config()
"""


@pytest.mark.parametrize("name", LANE_MODES)
@pytest.mark.parametrize("case", UNSAFE)
def test_process_wide_patch_fails_the_test(pytester, name, case):
    pytester.makepyfile(appcfg=APPCFG)
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
            import os
            os.environ["LANES_Y"] = "1"
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


def test_pytest_s_own_patches_are_not_guarded(pytester, monkeypatch):
    # pytest's unittest plugin monkeypatches twisted's Failure.__init__ around every
    # test once twisted.trial is imported (twisted <= 24): the guard failed every test,
    # outside its call phase, as an INTERNALERROR. pytest's own patches
    # are its business.
    site = pytester.mkdir("site")
    for path, text in {"twisted/__init__.py": "", "twisted/python/__init__.py": "",
                       "twisted/python/failure.py": "class Failure:\n    def __init__(self, *a, **k): pass\n",
                       "twisted/trial/__init__.py": "", "twisted/trial/unittest.py": "class TestCase: pass\n",
                       "twisted-24.3.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: twisted\nVersion: 24.3.0\n",
                       "twisted-24.3.0.dist-info/RECORD": ""}.items():
        (site / path).parent.mkdir(parents=True, exist_ok=True)
        (site / path).write_text(text)
    monkeypatch.setenv("PYTHONPATH", str(site))
    pytester.makepyfile("""
        import twisted.trial.unittest
        import pytest
        @pytest.mark.parametrize("i", range(4))
        def test_plain(i):
            pass
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=4)


def test_pytester_is_guarded(pytester):
    # pytester changes the cwd and environment for the process: exempting all of pytest
    # (for its twisted support) let it do that under other lanes.
    pytester.makeconftest('pytest_plugins = ["pytester"]')
    pytester.makepyfile("""
        def test_uses_pytester(pytester):
            pass
    """)
    r = run(pytester, "--lanes", "2", timeout=60)
    r.stdout.fnmatch_lines([GUARD_MESSAGE])


def test_higher_scoped_fixture_patch_is_guarded_in_an_exclusive_test(pytester):
    # A module fixture set up by an exclusive test keeps its patch for the module's later,
    # non-exclusive tests while other lanes run (hybrid runs exclusive tests inline).
    pytester.makepyfile(test_m1="""
        import os, time, pytest
        @pytest.fixture(scope="module")
        def env():
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("LANES_LEAK", "1")
                yield
        @pytest.mark.lanes_exclusive
        def test_1(env):
            pass
        def test_2(env):
            time.sleep(0.5)
    """)
    r = run(pytester, "-n", "1", "--lanes", "2", "--dist", "loadscope", timeout=60)
    r.stdout.fnmatch_lines(["*module-scoped fixture*"])


def test_factory_made_module_class_is_shared(pytester):
    pytester.makepyfile(appcls="""
        def _make():
            class Settings:
                timeout = 1
            return Settings
        Settings = _make()
    """)
    pytester.makepyfile("""
        from unittest import mock
        import appcls
        def test_it():
            with mock.patch.object(appcls.Settings, "timeout", 2):
                pass
    """)
    r = run(pytester, "--lanes", "2", timeout=60)
    r.stdout.fnmatch_lines([GUARD_MESSAGE])


def test_marker_exempts_broader_scoped_fixture_patches(pytester):
    # The marker says this test's patches are safe; it stopped covering its module- and
    # class-scoped fixtures when the scope check moved first.
    pytester.makepyfile(helper_mod="VALUE = 0\n")
    pytester.makepyfile("""
        import pytest
        from unittest import mock
        import helper_mod
        pytestmark = pytest.mark.lanes_allow_patches
        @pytest.fixture(scope="module", autouse=True)
        def mod_patch():
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(helper_mod, "VALUE", 1)
                yield
        class TestC:
            @pytest.fixture(scope="class", autouse=True)
            def cls_patch(self):
                with mock.patch.object(helper_mod, "VALUE", 2):
                    yield
            def test_a(self): pass
            def test_b(self): pass
        def test_c(): pass
        def test_d(): pass
    """)
    run(pytester, "--lanes", "3", "--lanes-dist", "loadscope", timeout=60).assert_outcomes(passed=4)


def test_class_made_with_type_in_a_test_is_local(pytester):
    pytester.makepyfile("""
        from unittest import mock
        def test_it():
            Fake = type("Fake", (), {"x": 0})
            with mock.patch.object(Fake, "x", 1):
                assert Fake.x == 1
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=1)


def test_nested_module_class_is_shared(pytester):
    pytester.makepyfile(appnest="class Outer:\n    class Inner:\n        x = 0\n")
    pytester.makepyfile("""
        from unittest import mock
        import appnest
        def test_it():
            with mock.patch.object(appnest.Outer.Inner, "x", 1):
                pass
    """)
    r = run(pytester, "--lanes", "2", timeout=60)
    r.stdout.fnmatch_lines([GUARD_MESSAGE])


def test_module_level_instance_is_shared(pytester):
    # A settings singleton held by a module is shared by every lane, like a class; the
    # guard let monkeypatch.setattr(settings_mod.settings, ...) through.
    pytester.makepyfile(settings_mod="class _S:\n    DEBUG = False\nsettings = _S()\n")
    pytester.makepyfile("""
        import settings_mod
        def test_patcher(monkeypatch):
            monkeypatch.setattr(settings_mod.settings, "DEBUG", True)
    """)
    r = run(pytester, "--lanes", "2", timeout=60)
    r.stdout.fnmatch_lines([GUARD_MESSAGE])


def test_environment_set_in_pytest_configure_is_fine(pytester):
    # Before the lanes start, nothing else runs: the recommended place for run-wide values.
    pytester.makeconftest("""
        import os
        def pytest_configure(config):
            os.environ["LANES_RUN"] = "1"
    """)
    pytester.makepyfile("""
        import os, pytest
        @pytest.mark.parametrize("i", range(4))
        def test_it(i):
            assert os.environ["LANES_RUN"] == "1"
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=4)


def test_direct_environment_write_message_names_the_subprocess_hazard(pytester):
    pytester.makepyfile(UNSAFE["environ_direct_write"])
    r = run(pytester, "--lanes", "2", timeout=60)
    r.stdout.re_match_lines([r".*a write to os\.environ\['LANES_X'\] patches process-wide state.*"
                             r"breaks subprocesses.*Bad address"])


# ---------------------------------------------------------------- no false failures
FORKED = """
import os, multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
def child():
    os.environ["CHILD_ONLY"] = "1"      # the child's own environment
    os.chdir("/")
def work(x):
    os.environ["W"] = str(x)
    return x
def test_mp_fork():
    p = mp.get_context("fork").Process(target=child)
    p.start(); p.join()
    assert p.exitcode == 0
def test_pool_fork():
    with ProcessPoolExecutor(2, mp_context=mp.get_context("fork")) as ex:
        assert list(ex.map(work, range(3))) == [0, 1, 2]
def test_raw_fork():
    pid = os.fork()
    if pid == 0:
        try:
            os.environ["X"] = "1"
            os._exit(0)
        except BaseException:
            os._exit(3)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
"""


@pytest.mark.parametrize("name", LANE_MODES)
def test_forked_child_owns_its_environment(pytester, name):
    # A child made by fork inherits the lane and the audit hook: its private environment
    # write failed.
    pytester.makepyfile(FORKED)
    run(pytester, *LANE_MODES[name], timeout=60).assert_outcomes(passed=3)


def test_chdir_to_the_current_directory_is_not_a_change(pytester):
    # pytest-cov's no_cover pauses coverage inside os.chdir(topdir) and back: the same directory.
    pytester.makepyfile("""
        import contextlib, os
        def test_it():
            os.chdir(os.getcwd())
            with contextlib.chdir("."):
                pass
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=1)


@pytest.mark.parametrize("name", LANE_MODES)
def test_pytest_cov_keeps_working(pytester, name):
    pytest.importorskip("pytest_cov")
    pytester.makepyfile(mod="def f(x):\n    return x + 1\n", test_x="""
        import pytest, mod
        @pytest.mark.no_cover
        def test_nc(): assert mod.f(1) == 2
        def test_fixture(no_cover): assert mod.f(2) == 3
        def test_cov(): assert mod.f(3) == 4
    """)
    r = run(pytester, *LANE_MODES[name], "--cov=mod", "--cov-report=term", "--cov-context=test", timeout=120)
    r.assert_outcomes(passed=3)
    r.stdout.fnmatch_lines(["mod.py *100%*"])


def test_import_time_environment_default_is_allowed(pytester):
    # A library that sets a default at import (thread-count knobs) failed whichever test
    # imported it first: an innocent test, chosen by timing.
    pytester.makepyfile(knobs="import os\nos.environ.setdefault('LANES_KNOB', '1')\n", test_x="""
        import time, pytest
        @pytest.mark.parametrize("i", range(4))
        def test_it(i):
            time.sleep(0.05)
            import knobs
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=4)


@pytest.mark.parametrize("form", ["patch_dict", "setitem"])
def test_patch_of_a_module_held_dict_is_guarded(pytester, form):
    # By dotted path it was guarded; by the object itself, or through setitem, not.
    body = {"patch_dict": "with mock.patch.dict(cfg.CONFIG, {'endpoint': 'stub'}): pass",
            "setitem": "monkeypatch.setitem(cfg.CONFIG, 'endpoint', 'stub')"}[form]
    pytester.makepyfile(cfg="CONFIG = {'endpoint': 'prod'}\n", test_d=f"""
        from unittest import mock
        import cfg
        def test_it(monkeypatch):
            {body}
    """)
    r = run(pytester, "--lanes", "2", timeout=60)
    r.assert_outcomes(failed=1)
    r.stdout.fnmatch_lines([GUARD_MESSAGE])


def test_environment_write_by_an_exempt_plugin(pytester):
    # The frame walk stopped at lanes' own os.environ class, so pytest-cov's writes
    # (COV_CORE_CONTEXT, pytest-cov < 7) were judged as the test's own.
    pytester.makepyfile(test_ex="""
        import os
        CODE = compile("import os\\nos.environ['COV_CORE_CONTEXT'] = 'x'\\n"
                       "os.environ.setdefault('COV_OTHER', 'y')\\n", "fake_cov.py", "exec")
        def test_env_write_from_exempt_module():
            exec(CODE, {"__name__": "pytest_cov.engine"})
    """)
    run(pytester, "--lanes", "2", timeout=60).assert_outcomes(passed=1)

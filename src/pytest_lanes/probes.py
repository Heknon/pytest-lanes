"""Fail closed: check every pytest/pluggy/xdist internal we rely on, before running.

Each check returns a problem description, or ``None`` when the touchpoint looks
the way the code in ``isolation.py``, ``capture.py``, ``hookrouting.py`` and
``scheduling.py`` expects. Any problem stops the run with ``UsageError`` rather
than letting lanes run incorrectly. The IDs match the touchpoint tables in
CLAUDE.md and DESIGN.md.

X1 is also checked on each scheduler instance (``scheduling.make_scheduler``).
X2 (hybrid worker) is resolved where it is used. X3 and X4 (hybrid controller)
are checked by ``check_controller_touchpoints``.

Not probed (a gap carried over from the original single-file plugin): P1
``session._setupstate`` and P5 ``item._nodeid``. The contract tests cover both.
"""
from __future__ import annotations

import sys

import pytest


def _p2_fixture_caches(config):
    from _pytest.fixtures import FixtureDef

    problems = []
    names = set(FixtureDef.__init__.__code__.co_names)
    if not FixtureDef.__dict__.get("_lanes_patched") and not {"cached_result", "_finalizers"} <= names:
        problems.append("P2 FixtureDef.cached_result/_finalizers")
    if getattr(FixtureDef, "__slots__", None):
        problems.append("P2 FixtureDef now uses __slots__")
    return "\n  ".join(problems) or None


def _p3_logging(config):
    lp = config.pluginmanager.get_plugin("logging-plugin")
    if lp is not None and not (hasattr(lp, "caplog_handler") and hasattr(lp, "report_handler")):
        return "P3 LoggingPlugin handler attributes"
    return None


def _p8_logger_dict(config):
    import logging

    if type(getattr(logging.Logger.manager, "loggerDict", None)) is not dict:
        return "P8 logging.Logger.manager.loggerDict is not a plain dict"
    return None


def _p9_doctest_item(config):
    if config.pluginmanager.get_plugin("doctest") is None:
        return None
    try:
        from _pytest.doctest import DoctestItem  # noqa: F401
    except ImportError:
        return "P9 _pytest.doctest.DoctestItem"
    return None


def _c1_rerunfailures_client(config):
    from .compat import RERUN_DB_METHODS, rerunfailures_client

    db = rerunfailures_client(config)
    if db is not None and not (hasattr(db, "sock") and hasattr(db, "_get") and hasattr(db, "_set")):
        return f"C1 pytest-rerunfailures ClientStatusDB no longer has sock/{'/'.join(RERUN_DB_METHODS)}"
    return None


def _p10_worker_identity(config):
    import xdist

    if "__slots__" in vars(type(config)):
        return "P10 pytest's Config class now uses __slots__"
    readers = (xdist.get_xdist_worker_id, xdist.is_xdist_worker)
    if not all("workerinput" in (*f.__code__.co_names, *f.__code__.co_consts) for f in readers):
        return "P10 xdist's worker id no longer comes from config.workerinput"
    return None


def _p11_warnings_recorder(config):
    if getattr(sys.flags, "context_aware_warnings", False):
        return None
    from _pytest.recwarn import WarningsRecorder

    if "__enter__" not in vars(WarningsRecorder):
        return "P11 _pytest.recwarn.WarningsRecorder.__enter__"
    return None


def _p4_hookexec(config):
    if not hasattr(config.pluginmanager, "_inner_hookexec"):
        return "P4 pluggy PluginManager._inner_hookexec"
    return None


def _p6_current_test_var(config):
    from _pytest import runner

    if not callable(getattr(runner, "_update_current_test_var", None)):
        return "P6 _pytest.runner._update_current_test_var"
    return None


def _p7_basetemp(config):
    if config.pluginmanager.get_plugin("tmpdir") is None:
        return None
    tpf = getattr(config, "_tmp_path_factory", None)
    if tpf is None or not callable(getattr(tpf, "getbasetemp", None)) or not hasattr(tpf, "__dict__") \
            or not {"_given_basetemp", "_basetemp"} <= set(vars(tpf)):
        return "P7 config._tmp_path_factory (getbasetemp, _given_basetemp, _basetemp)"
    legacy = getattr(config, "_tmpdirhandler", None)
    if legacy is not None and "_tmppath_factory" not in vars(legacy):
        return "P7 config._tmpdirhandler._tmppath_factory"
    return None


def _x1_xdist_scheduler_api(config):
    try:
        from xdist.remote import Producer  # noqa: F401
        from xdist.scheduler import LoadScopeScheduling  # noqa: F401
    except ImportError as e:
        return f"X1 xdist scheduler API: {e}"
    return None


# ---- environment: process-global hooks that are not thread-safe --------------------
def _warnings(config):
    if config.pluginmanager.get_plugin("warnings") is not None \
            and not getattr(sys.flags, "context_aware_warnings", False):
        return ("warnings plugin active but catch_warnings is not thread-safe here: "
                "use Python>=3.14 with -X context_aware_warnings=1, or -p no:warnings")
    return None


PYTEST_TIMEOUT_REFUSED = (
    "pytest-timeout is active with a timeout, and in single-process lanes it cannot use "
    "signals: on a timeout it ends the whole process, every lane with it, with no reports. "
    "Run hybrid (-n P --lanes M, where xdist replaces the worker) or turn the timeout off.")


def _pytest_timeout(config):
    if hasattr(config, "workerinput") or config.pluginmanager.get_plugin("timeout") is None:
        return None                     # hybrid worker: xdist replaces a killed process
    try:
        from pytest_timeout import get_env_settings
        timeout = get_env_settings(config).timeout
    except Exception as e:  # fail closed: we cannot tell whether a timeout is set
        return f"pytest-timeout is active and its settings could not be read ({e!r})"
    return PYTEST_TIMEOUT_REFUSED if timeout else None


def _faulthandler_timeout(config):
    if config.pluginmanager.get_plugin("faulthandler") is None:
        return None
    if float(config.getini("faulthandler_timeout") or 0) > 0:
        return ("faulthandler_timeout does not work under lanes: its timer is process-wide and "
                "every test restarts or cancels it, so it never fires for the test that hangs. "
                "Remove it for lanes runs.")
    return None


def _per_test_global_hooks(config):
    # pytest <= 8.3.5 swaps these global hooks around every test phase; newer pytest
    # installs them once (and has a module-level pytest_configure to do it).
    problems = []
    for name, hook in (("threadexception", "threading.excepthook"),
                       ("unraisableexception", "sys.unraisablehook")):
        mod = config.pluginmanager.get_plugin(name)
        if mod is not None and not hasattr(mod, "pytest_configure"):
            problems.append(f"this pytest's {name} plugin swaps {hook} per test (races across lanes): "
                            f"upgrade pytest or pass -p no:{name}")
    return "\n  ".join(problems) or None


CHECKS = (_p4_hookexec, _p2_fixture_caches, _p3_logging, _warnings, _p6_current_test_var,
          _p7_basetemp, _p8_logger_dict, _p9_doctest_item, _per_test_global_hooks,
          _p10_worker_identity, _p11_warnings_recorder, _x1_xdist_scheduler_api,
          _c1_rerunfailures_client,
          _pytest_timeout, _faulthandler_timeout)


def check_touchpoints(config) -> None:
    """Every process that runs tests on lanes (single-process, hybrid worker)."""
    _raise_if([check(config) for check in CHECKS])


# ---- hybrid controller -----------------------------------------------------------
def _x3_handle_crashitem(config):
    from xdist.dsession import DSession

    if not callable(getattr(DSession, "handle_crashitem", None)):
        return "X3 xdist DSession.handle_crashitem"
    return None


def _x4_worker_attributes(config):
    # LaneProxy mirrors these WorkerController attributes (set in the code checked here).
    from xdist.dsession import DSession
    from xdist.workermanage import WorkerController

    if not ("workerinput" in WorkerController.__init__.__code__.co_names
            and "workerinfo" in DSession.worker_workerready.__code__.co_names
            and "workeroutput" in WorkerController.process_from_remote.__code__.co_names):
        return "X4 xdist WorkerController.workerinput/workerinfo/workeroutput"
    return None


CONTROLLER_CHECKS = (_x3_handle_crashitem, _x4_worker_attributes, _faulthandler_timeout)


def check_controller_touchpoints(config) -> None:
    """The hybrid controller, which runs no tests itself."""
    _raise_if([check(config) for check in CONTROLLER_CHECKS])


def _raise_if(problems) -> None:
    problems = [p for p in problems if p]
    if problems:
        raise pytest.UsageError("pytest-lanes refuses to run (fail-closed):\n  " + "\n  ".join(problems))

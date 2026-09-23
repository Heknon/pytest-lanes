"""Fail closed: check every pytest/pluggy/xdist internal we rely on, before running.

Each check returns a problem description, or ``None`` when the touchpoint looks
the way the code in ``isolation.py``, ``capture.py``, ``hookrouting.py`` and
``scheduling.py`` expects. Any problem stops the run with ``UsageError`` rather
than letting lanes run incorrectly. The IDs match the touchpoint tables in
CLAUDE.md and DESIGN.md.

X1 is also checked on each scheduler instance (``scheduling.make_scheduler``).
X2 and X3 exist only in hybrid mode and are resolved where they are used.

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
    if tpf is None or not callable(getattr(tpf, "getbasetemp", None)) or not hasattr(tpf, "__dict__"):
        return "P7 config._tmp_path_factory.getbasetemp"
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
          _p7_basetemp, _per_test_global_hooks, _x1_xdist_scheduler_api)


def check_touchpoints(config) -> None:
    problems = [p for p in (check(config) for check in CHECKS) if p]
    if problems:
        raise pytest.UsageError("pytest-lanes refuses to run (fail-closed):\n  " + "\n  ".join(problems))

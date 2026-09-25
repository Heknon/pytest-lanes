"""pytest-threadlanes entry point: options, and choosing a mode.

pytest-threadlanes runs pytest-xdist's own schedulers on thread "lanes":

    pytest -n 8               plain xdist (untouched)
    pytest --lanes 200        1 process x 200 lanes       -> single.SingleProcessSession
    pytest -n 8 --lanes 25    8 processes x 25 lanes      -> controller.LanesController
                                                             + worker.HybridWorkerSession

Module map:

    lane.py          ThreadNode: one lane, and the LANE contextvar
    runner.py        LaneRunner: lane threads, main-thread event pump, exclusivity lock
    single.py        --lanes N: this process plays xdist controller and workers
    worker.py        -n P --lanes M, worker process: M lanes behind xdist's channel
    controller.py    -n P --lanes M, controller: LaneMux / LaneProxy around xdist's DSession
    scheduling.py    building the real xdist scheduler; loadgroup nodeid suffix (X1, P5)
    isolation.py     per-lane pytest state, the patch guard (P1, P2, P6, P7, P10-P12, P14)
    capture.py       per-lane stdout/stderr, redirects and logging (P3, P8, P13, P15)
    hookrouting.py   the 4 controller hooks replayed on the main thread (P4)
    compat.py        shims for third-party plugins that assume one test per process (C1, C2)
    integrity.py     run-time check that reports match what the lanes ran
    detector/        --lanes-detect: which tests change process-wide state (debugging tool)
    probes.py        fail-closed startup checks of every internal we touch

This module must stay free of logic: pytest registers it as a plugin, so any
``pytest_*`` name defined or imported here becomes a hook implementation.
"""
from __future__ import annotations

import pytest

from .controller import LanesController
from .detector import SharedStateDetector, refuse_concurrent
from .probes import check_controller_touchpoints, check_touchpoints
from .scheduling import SUPPORTED_DIST
from .single import SingleProcessSession
from .worker import HybridWorkerSession

DEFAULT_EXCLUSIVE = ("capsys", "capsysbinary", "capfd", "capfdbinary", "capteesys", "recwarn",
                     "no_cover")   # pytest-cov: pauses coverage for the whole process


def pytest_addoption(parser):
    g = parser.getgroup("lanes")
    g.addoption("--lanes", type=int, default=None, metavar="N",
                help="run tests on N thread lanes in this process, scheduled by pytest-xdist schedulers")
    g.addoption("--lanes-dist", default=None, choices=SUPPORTED_DIST,
                help="built-in xdist scheduler for single-process mode when no "
                     "pytest_xdist_make_scheduler implementation returns one "
                     "(default: xdist's --dist if given, else load)")
    g.addoption("--lanes-xdist-node-hooks", action="store_true", default=False,
                help="fire xdist node hooks (testnodeready/down) to plugins in lanes_node_hook_plugins")
    parser.addini("lanes_node_hook_plugins", "plugin names (substring match) that receive xdist "
                  "node hooks; others never see them", type="args", default=["conftest"])
    parser.addini("lanes_exclusive_fixtures", "fixtures forcing a test into the serial phase",
                  type="args", default=list(DEFAULT_EXCLUSIVE))
    g.addoption("--lanes-allow-patches", action="store_true", default=False,
                help="allow process-wide patches (mock.patch or monkeypatch of a module or class, "
                     "environment, chdir, sys.path) in tests that are not lanes_exclusive")
    parser.addini("lanes_allow_patches", "same as --lanes-allow-patches", type="bool", default=False)
    parser.addini("lanes_interrupt_grace", "seconds to wait after Ctrl-C for interrupted lanes to "
                  "run their teardown", default="30")

    g.addoption("--lanes-detect", action="store_true", default=False,
                help="run tests one at a time and report which ones change process-wide state "
                     "that concurrent lanes would share")
    g.addoption("--lanes-detect-report", default=None, metavar="PATH",
                help="also write the --lanes-detect report as JSON to PATH")
    parser.addini("lanes_detect_ignore", "--lanes-detect: paths (fnmatch patterns) that are "
                  "shared on purpose, e.g. module:myinfra.clients._CACHE", type="linelist", default=[])
    parser.addini("lanes_detect_modules", "--lanes-detect: extra packages to inspect besides "
                  "the modules under the rootdir, e.g. an installed infrastructure package",
                  type="args", default=[])
    parser.addini("lanes_detect_max_depth", "--lanes-detect: how deep to follow attributes",
                  default="12")
    parser.addini("lanes_detect_max_nodes", "--lanes-detect: most values per snapshot",
                  default="200000")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    ns = early_config.known_args_namespace
    if getattr(ns, "lanes", None):
        ns.capture = "no"  # lanes do their own per-test capture (capture.py)
    return (yield)


@pytest.hookimpl(trylast=True)  # after builtins configure (the P7 probe needs tmpdir's factory)
def pytest_configure(config):
    config.addinivalue_line("markers", "lanes_exclusive: run in the serial phase, alone")
    config.addinivalue_line("markers", "lanes_allow_patches: this test's process-wide patches are "
                                       "safe (nothing another test runs uses what it patches)")
    lanes = config.getoption("lanes")
    if config.getoption("lanes_detect"):
        refuse_concurrent(config)
        config.pluginmanager.register(SharedStateDetector(config), "lanes-detector")
        return
    if lanes is not None and lanes < 0:
        raise pytest.UsageError(f"--lanes must be 0 (off) or a positive number of lanes, not {lanes}")
    if not lanes:
        return
    if not config.pluginmanager.hasplugin("xdist"):
        raise pytest.UsageError("--lanes drives pytest-xdist's schedulers; install pytest-xdist")
    if getattr(config.option, "usepdb", False):
        raise pytest.UsageError("--lanes is incompatible with --pdb (as is xdist)")

    pm = config.pluginmanager
    if hasattr(config, "workerinput"):
        # Hybrid, worker side: this xdist worker process runs M lanes.
        check_touchpoints(config)
        pm.register(HybridWorkerSession(config), "lanes-session")
    elif getattr(config.option, "numprocesses", None) or getattr(config.option, "tx", None):
        # Hybrid, controller side: real xdist DSession and processes; lanes are virtual nodes.
        if config.getoption("lanes_dist"):
            raise pytest.UsageError("--lanes-dist chooses the scheduler of single-process lanes; "
                                    "with -n it would be ignored: use xdist's --dist")
        check_controller_touchpoints(config)
        pm.register(LanesController(config), "lanes-controller")   # passes -X flags to workers
    else:
        if getattr(config.option, "trace", False):
            raise pytest.UsageError("--lanes is incompatible with --trace: pdb would block a lane")
        check_touchpoints(config)
        pm.register(SingleProcessSession(config), "lanes-session")

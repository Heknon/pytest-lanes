"""pytest-lanes entry point: options, and choosing a mode.

pytest-lanes runs pytest-xdist's own schedulers on thread "lanes":

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
    scheduling.py    building the real xdist scheduler; loadgroup nodeid suffix
    isolation.py     per-lane pytest state (touchpoints P1, P2, P6, P7)
    capture.py       per-lane stdout/stderr and logging (P3)
    hookrouting.py   the 4 controller hooks replayed on the main thread (P4)
    probes.py        fail-closed startup checks of every internal we touch

This module must stay free of logic: pytest registers it as a plugin, so any
``pytest_*`` name defined or imported here becomes a hook implementation.
"""
from __future__ import annotations

import pytest

from .controller import LanesController
from .probes import check_touchpoints
from .scheduling import SUPPORTED_DIST
from .single import SingleProcessSession
from .worker import HybridWorkerSession

DEFAULT_EXCLUSIVE = ("capsys", "capsysbinary", "capfd", "capfdbinary", "capteesys", "recwarn")


def pytest_addoption(parser):
    g = parser.getgroup("lanes")
    g.addoption("--lanes", type=int, default=None, metavar="N",
                help="run tests on N thread lanes in this process, scheduled by pytest-xdist schedulers")
    g.addoption("--lanes-dist", default="load", choices=SUPPORTED_DIST,
                help="built-in xdist scheduler to use when no pytest_xdist_make_scheduler "
                     "implementation returns one (default: load, like xdist)")
    g.addoption("--lanes-xdist-node-hooks", action="store_true", default=False,
                help="fire xdist node hooks (testnodeready/down) to plugins in lanes_node_hook_plugins")
    parser.addini("lanes_node_hook_plugins", "plugin names (substring match) that receive xdist "
                  "node hooks; others never see them", type="args", default=["conftest"])
    parser.addini("lanes_exclusive_fixtures", "fixtures forcing a test into the serial phase",
                  type="args", default=list(DEFAULT_EXCLUSIVE))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    ns = early_config.known_args_namespace
    if getattr(ns, "lanes", None):
        ns.capture = "no"  # lanes do their own per-test capture (capture.py)
    return (yield)


@pytest.hookimpl(trylast=True)  # after builtins configure (the P7 probe needs tmpdir's factory)
def pytest_configure(config):
    config.addinivalue_line("markers", "lanes_exclusive: run in the serial phase, alone")
    if not config.getoption("lanes"):
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
        pm.register(LanesController(config), "lanes-controller")
    else:
        check_touchpoints(config)
        pm.register(SingleProcessSession(config), "lanes-session")

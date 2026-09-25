"""Getting a real xdist scheduler to drive lanes (touchpoints X1 and P5).

Lanes never make scheduling decisions of their own. They build the scheduler
the same way xdist does, through the ``pytest_xdist_make_scheduler`` hook, and
fall back to the built-in class named by ``--lanes-dist``. The same
user-supplied scheduler therefore works under ``-n``, ``--lanes`` and hybrid.
"""
from __future__ import annotations

import pytest

SUPPORTED_DIST = ("load", "loadscope", "loadfile", "loadgroup")


def builtin_dist(config) -> str:
    """The built-in scheduler to fall back to: --lanes-dist, else xdist's --dist, else load."""
    dist = config.getoption("lanes_dist") or config.getoption("dist", "no")
    if dist in (None, "no"):
        return "load"
    if dist not in SUPPORTED_DIST:
        raise pytest.UsageError(f"--dist {dist} is not supported by lanes "
                                f"(supported: {', '.join(SUPPORTED_DIST)})")
    return dist
UNSUPPORTED_SCHEDULERS = ("EachScheduling", "WorkStealingScheduling")

#: X1: what lanes call on a scheduler. Probed on each instance, never by import
#: name (xdist 3.6.1 lacks some module-level helpers).
SCHEDULER_PROTOCOL = ("add_node", "add_node_collection", "schedule", "mark_test_complete",
                      "tests_finished", "collection_is_completed")


def reject_unsupported(sched) -> None:
    """Refuse each/worksteal schedulers, subclasses included (by the class hierarchy's
    names: xdist's classes are not importable by one path on every version)."""
    names = {cls.__name__ for cls in type(sched).__mro__}
    unsupported = names & set(UNSUPPORTED_SCHEDULERS)
    if unsupported:
        raise pytest.UsageError(f"{type(sched).__name__} ({', '.join(sorted(unsupported))}) "
                                f"is not supported by lanes")


def make_scheduler(config, numnodes: int):
    """Build the run's scheduler for ``numnodes`` lanes in this process."""
    from xdist.remote import Producer
    from xdist.scheduler import (
        LoadFileScheduling,
        LoadGroupScheduling,
        LoadScheduling,
        LoadScopeScheduling,
    )

    builtin = {"load": LoadScheduling, "loadscope": LoadScopeScheduling,
               "loadfile": LoadFileScheduling, "loadgroup": LoadGroupScheduling}
    opt = config.option
    # xdist schedulers size themselves from --tx: present N lanes as N nodes, then restore.
    saved = getattr(opt, "tx", None)
    opt.tx = [f"{numnodes}*popen"]
    try:
        log = Producer("lanessched", enabled=False)
        sched = config.hook.pytest_xdist_make_scheduler(config=config, log=log)
        if sched is None:
            sched = builtin[builtin_dist(config)](config, log)
    finally:
        opt.tx = saved

    missing = [m for m in SCHEDULER_PROTOCOL if not hasattr(sched, m)]
    if missing:
        raise pytest.UsageError(f"pytest-threadlanes: scheduler {type(sched).__name__} lacks {missing}")
    sched.numnodes = numnodes
    reject_unsupported(sched)
    return sched


class _Loadgroup:
    """The one config value xdist's worker hook reads: ``--dist loadgroup`` is on."""

    @staticmethod
    def getvalue(name):
        return name == "loadgroup"


def add_group_suffix(items) -> None:
    """P5: append ``@group`` to xdist_group-marked nodeids with xdist's own worker code
    (``WorkerInteractor.pytest_collection_modifyitems``, which does not use ``self``), so
    nodeids match an ``-n --dist loadgroup`` run of the installed xdist: 3.6 takes the
    closest mark, 3.8 every mark."""
    from xdist.remote import WorkerInteractor

    WorkerInteractor.pytest_collection_modifyitems(None, _Loadgroup(), items)

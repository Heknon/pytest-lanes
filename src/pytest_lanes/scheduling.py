"""Getting a real xdist scheduler to drive lanes (touchpoints X1 and P5).

Lanes never make scheduling decisions of their own. They build the scheduler
the same way xdist does, through the ``pytest_xdist_make_scheduler`` hook, and
fall back to the built-in class named by ``--lanes-dist``. The same
user-supplied scheduler therefore works under ``-n``, ``--lanes`` and hybrid.
"""
from __future__ import annotations

import pytest

SUPPORTED_DIST = ("load", "loadscope", "loadfile", "loadgroup")
UNSUPPORTED_SCHEDULERS = ("EachScheduling", "WorkStealingScheduling")

#: X1: what lanes call on a scheduler. Probed on each instance, never by import
#: name (xdist 3.6.1 lacks some module-level helpers).
SCHEDULER_PROTOCOL = ("add_node", "add_node_collection", "schedule", "mark_test_complete",
                      "tests_finished", "collection_is_completed")


def reject_unsupported(sched) -> None:
    name = type(sched).__name__
    if name in UNSUPPORTED_SCHEDULERS:
        raise pytest.UsageError(f"{name} is not supported by lanes")


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
            sched = builtin[config.getoption("lanes_dist")](config, log)
    finally:
        opt.tx = saved

    missing = [m for m in SCHEDULER_PROTOCOL if not hasattr(sched, m)]
    if missing:
        raise pytest.UsageError(f"pytest-lanes: scheduler {type(sched).__name__} lacks {missing}")
    sched.numnodes = numnodes
    reject_unsupported(sched)
    return sched


def add_group_suffix(items) -> None:
    """P5: append ``@group`` to xdist_group-marked nodeids, exactly as xdist's
    worker does under loadgroup, so nodeids (and everything keyed on them) match
    an ``-n --dist loadgroup`` run."""
    for item in items:
        groups = set()
        for mark in item.iter_markers("xdist_group"):
            groups.add(str(mark.args[0] if mark.args else mark.kwargs.get("name", "default")))
        if groups:
            item._nodeid = f"{item.nodeid}@{'_'.join(sorted(groups))}"

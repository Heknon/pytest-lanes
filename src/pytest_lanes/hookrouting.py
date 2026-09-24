"""Route controller-side hooks from lanes to the main thread (touchpoint P4).

xdist runs every hook in the worker, except four that it forwards to the
controller: the ``ROUTED`` set below. Lanes keep exactly that split. When one of
these hooks is called on a lane, it is queued as a ``HookCall`` instead of run,
and the main thread replays it in order. Reporters (terminal, junitxml,
report-log, ...) therefore see one thread and a well-ordered stream, as they
would on an xdist controller.

One exception keeps xdist's worker semantics: in a worker, pytest's own ``Session``
counts failures synchronously in ``pytest_runtest_logreport`` (``testsfailed``,
``shouldfail`` for -x/--maxfail), and the runner reads ``shouldfail`` before tearing
the failing test down. So the Session's implementation runs on the lane, at once
and under a lock, and is left out of the replay.

The hook call is intercepted at pluggy's ``PluginManager._inner_hookexec``, the
same slot that pluggy's public ``add_hookcall_monitoring`` wraps.
"""
from __future__ import annotations

import threading
from typing import NamedTuple

from .lane import LANE

#: Exactly the hooks xdist forwards worker -> controller.
ROUTED = frozenset({
    "pytest_runtest_logstart",
    "pytest_runtest_logreport",
    "pytest_runtest_logfinish",
    "pytest_warning_recorded",
})


class HookCall(NamedTuple):
    name: str
    impls: list
    kwargs: dict
    firstresult: bool
    lane: str  # the id of the lane that made the call


class ControllerHookRouter:
    """Context manager that installs the interception; ``replay()`` runs a queued call.

    ``set_report_node``: in single-process mode ``report.node`` is set to the lane,
    as xdist's controller sets it to the worker. A hybrid worker must not set it,
    because its reports are serialized to the controller; ``report.lane_id`` (a
    string) is set in both modes. ``ledger`` (integrity.py) sees each routed call
    on the lane that makes it.
    """

    def __init__(self, pluginmanager, events, session, *, set_report_node: bool, ledger) -> None:
        self._pm = pluginmanager
        self._events = events
        self._session = session
        self._set_report_node = set_report_node
        self._ledger = ledger
        self._inner = None

    def __enter__(self):
        self._inner = inner = self._pm._inner_hookexec
        events, session, set_report_node = self._events, self._session, self._set_report_node
        ledger = self._ledger
        session_lock = threading.Lock()

        def hookexec(name, impls, kwargs, firstresult):
            lane = LANE.get()
            if lane is None or name not in ROUTED:
                return inner(name, impls, kwargs, firstresult)
            ledger.emitted(lane, name, kwargs)
            if name == "pytest_runtest_logreport":
                report = kwargs["report"]
                report.lane_id = lane.gateway.id
                if set_report_node:
                    report.node = lane
                on_lane = [i for i in impls if i.plugin is session]
                if on_lane:
                    with session_lock:
                        inner(name, on_lane, kwargs, firstresult)
                    impls = [i for i in impls if i.plugin is not session]
            events.put(HookCall(name, impls, kwargs, firstresult, lane.gateway.id))
            return None if firstresult else []

        self._pm._inner_hookexec = hookexec
        return self

    def __exit__(self, *exc) -> None:
        self._pm._inner_hookexec = self._inner

    def replay(self, call: HookCall):
        return self._inner(call.name, call.impls, call.kwargs, call.firstresult)

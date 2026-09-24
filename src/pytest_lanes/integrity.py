"""Run-time integrity check: a report stream that does not add up fails the run.

A race that crashes gets noticed. The dangerous kind leaves the run green while
a report belongs to another test, or an item's reports are lost. xdist gets this
structure for free from one worker process running one item at a time; lanes
rebuild it from a shared queue, so every run checks it:

* on the lane, each routed hook (logstart, logreport, logfinish) names the item
  that lane is running;
* on the main thread, each lane's replayed stream nests correctly: logstart, then
  that item's reports, then its logfinish, before the next logstart;
* when a lane says an item is done, the replay has just shown that item's
  logstart and logfinish (the lane's hook calls precede its ``ItemDone`` in the
  same FIFO queue, so this is exact);
* in single-process mode, when the run was not stopped, every collected item
  finished exactly as many times as it was collected.

Any violation fails the run with ``IntegrityError`` (an INTERNALERROR, exit code 3)
listing what was wrong: the outcomes of such a run cannot be trusted.
"""
from __future__ import annotations

import threading
from collections import Counter

#: Hooks whose stream is checked; pytest_warning_recorded carries no reliable nodeid.
_CHECKED = ("pytest_runtest_logstart", "pytest_runtest_logreport", "pytest_runtest_logfinish")
_SHOWN = 20


class IntegrityError(Exception):
    pass


def _nodeid(name: str, kwargs: dict):
    if name not in _CHECKED:
        return None
    if name == "pytest_runtest_logreport":
        return kwargs["report"].nodeid
    return kwargs["nodeid"]


class Ledger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.violations: list[str] = []
        self._open: dict = {}         # lane id -> nodeid between its logstart and logfinish
        self._finished: dict = {}     # lane id -> the last nodeid it logfinished
        self.done: Counter = Counter()

    def _violation(self, message: str) -> None:
        with self._lock:
            self.violations.append(message)

    # On the lane thread, as a routed hook is called.
    def emitted(self, lane, name: str, kwargs: dict) -> None:
        nodeid = _nodeid(name, kwargs)
        if nodeid is None:
            return
        item = lane.current_item
        if item is None or item.nodeid != nodeid:
            running = item.nodeid if item is not None else "no test"
            self._violation(f"lane {lane.gateway.id} emitted {name} for {nodeid} "
                            f"while running {running}")

    # On the main thread, as a routed hook is replayed.
    def replayed(self, lane_id: str, name: str, kwargs: dict) -> None:
        nodeid = _nodeid(name, kwargs)
        if nodeid is None:
            return
        current = self._open.get(lane_id)
        if name == "pytest_runtest_logstart":
            if current is not None:
                self._violation(f"lane {lane_id}: logstart for {nodeid} before logfinish for {current}")
            self._open[lane_id] = nodeid
        elif name == "pytest_runtest_logreport":
            if current != nodeid:
                self._violation(f"lane {lane_id}: report for {nodeid} outside its logstart/logfinish "
                                f"(open: {current or 'none'})")
        else:
            if current != nodeid:
                self._violation(f"lane {lane_id}: logfinish for {nodeid} without its logstart "
                                f"(open: {current or 'none'})")
            self._open[lane_id] = None
            self._finished[lane_id] = nodeid

    # On the main thread, when a lane reports an item done.
    def item_done(self, lane_id: str, nodeid: str) -> None:
        current = self._open.get(lane_id)
        if current is not None:
            self._violation(f"lane {lane_id}: {nodeid} done while {current} has no logfinish")
            self._open[lane_id] = None
        if self._finished.pop(lane_id, None) != nodeid:
            self._violation(f"lane {lane_id}: {nodeid} finished without its logstart/logfinish")
        self.done[nodeid] += 1

    def check_complete(self, items) -> None:
        """Every collected item done as many times as collected (single-process, not stopped)."""
        expected = Counter(it.nodeid for it in items)
        if self.done == expected:
            return
        for nodeid in sorted(set(expected) | set(self.done)):
            if expected[nodeid] != self.done[nodeid]:
                self._violation(f"{nodeid}: collected {expected[nodeid]} time(s), "
                                f"ran {self.done[nodeid]} time(s)")

    def raise_if_violated(self) -> None:
        if not self.violations:
            return
        shown = self.violations[:_SHOWN]
        more = len(self.violations) - len(shown)
        lines = "\n".join(f"  - {v}" for v in shown) + (f"\n  ... and {more} more" if more else "")
        raise IntegrityError(
            "pytest-lanes integrity check failed: the reports of this run do not match what "
            "its lanes ran, so its outcomes cannot be trusted. Please report this with the "
            f"lines below.\n{lines}")

"""Per-lane output capture: stdout/stderr and logging (touchpoint P3).

pytest's own capture is process-wide, so lanes switch it off (``--capture=no``)
and capture per lane instead:

* ``sys.stdout``/``sys.stderr`` become ``_LaneStream`` objects that write to the
  current lane's buffers. The runner turns those buffers into report sections
  after each phase, as pytest's CaptureManager would.
* The logging plugin's ``caplog_handler`` and ``report_handler`` become
  ``_LogDispatch`` stand-ins that forward to the current lane's own copies.
  A permanent ``_LogRouter`` on the root logger does the actual emitting, so
  pytest attaching and detaching handlers concurrently from many lanes is harmless.

On the main thread (no lane), everything falls through to the real objects.
"""
from __future__ import annotations

import contextlib
import io
import logging
import sys

from .lane import LANE


# ------------------------------------------------------------------ stdout/stderr
class _LaneStream(io.TextIOBase):
    def __init__(self, real, attr: str) -> None:
        self._real = real
        self._attr = attr  # "out" or "err": the ThreadNode buffer to write to

    def write(self, s):
        lane = LANE.get()
        if lane is None:
            return self._real.write(s)
        return getattr(lane, self._attr).write(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def per_lane_std_streams():
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = _LaneStream(real_out, "out")
    sys.stderr = _LaneStream(real_err, "err")
    try:
        yield
    finally:
        sys.stdout, sys.stderr = real_out, real_err


# ------------------------------------------------------------------ logging (P3)
class _LogDispatch(logging.Handler):
    """Stands in for LoggingPlugin.caplog_handler / report_handler.

    ``catching_logs()`` may attach and detach it concurrently. Under a lane it
    never emits (the permanent ``_LogRouter`` does), so those races are harmless.
    """

    def __init__(self, key: str, fallback) -> None:
        self.__dict__["_key"] = key
        self.__dict__["_fallback"] = fallback
        super().__init__()

    def _t(self):
        lane = LANE.get()
        return self.__dict__["_fallback"] if lane is None else lane.log_handlers[self.__dict__["_key"]]

    level = property(lambda s: s._t().level, lambda s, v: None)
    records = property(lambda s: s._t().records)
    stream = property(lambda s: s._t().stream)
    formatter = property(lambda s: s._t().formatter, lambda s, v: None)

    def setLevel(self, level):
        self._t().setLevel(level)

    def reset(self):
        self._t().reset()

    def clear(self):
        self._t().clear()

    def handle(self, record):
        if LANE.get() is None:
            return self.__dict__["_fallback"].handle(record)
        return False

    def emit(self, record):  # pragma: no cover - handle() short-circuits
        pass


class _LogRouter(logging.Handler):
    """Permanently attached; delivers each record to the current lane's handlers."""

    def handle(self, record):
        lane = LANE.get()
        if lane is None:
            return False
        for h in lane.log_handlers.values():
            if record.levelno >= h.level:
                h.handle(record)
        return True

    def emit(self, record):  # pragma: no cover
        pass


@contextlib.contextmanager
def per_lane_logging(config):
    """Yields the plugin's original handlers ({"caplog": h, "report": h}) as templates
    for ``clone_log_handlers``, or ``{}`` when the logging plugin is disabled."""
    lp = config.pluginmanager.get_plugin("logging-plugin")
    if lp is None:
        yield {}
        return
    original = (lp.caplog_handler, lp.report_handler)
    templates = {"caplog": lp.caplog_handler, "report": lp.report_handler}
    lp.caplog_handler = _LogDispatch("caplog", lp.caplog_handler)
    lp.report_handler = _LogDispatch("report", lp.report_handler)
    root = logging.getLogger()
    if lp.log_level is not None:  # pre-lower, so catching_logs() restoring the level is a no-op
        root.setLevel(min(root.level, lp.log_level))
    router = _LogRouter()
    routed = [root] + [lg for lg in root.manager.loggerDict.values()
                       if isinstance(lg, logging.Logger) and not lg.propagate]
    for lg in routed:
        lg.addHandler(router)
    try:
        yield templates
    finally:
        lp.caplog_handler, lp.report_handler = original
        for lg in routed:
            lg.removeHandler(router)


def clone_log_handlers(templates: dict) -> dict:
    """Fresh handlers for one lane, configured like the logging plugin's own."""
    handlers = {}
    for key, tmpl in templates.items():
        h = type(tmpl)()
        h.setFormatter(tmpl.formatter)
        h.setLevel(tmpl.level)
        handlers[key] = h
    return handlers


# ------------------------------------------------------------------ per phase
def capture_phase(item, when):
    """Generator body for the setup/call/teardown hook wrappers: gives the lane
    fresh buffers, then adds them to the report as pytest's CaptureManager would."""
    lane = LANE.get()
    if lane is None:
        return (yield)
    lane.out, lane.err = io.StringIO(), io.StringIO()
    try:
        return (yield)
    finally:
        item.add_report_section(when, "stdout", lane.out.getvalue())
        item.add_report_section(when, "stderr", lane.err.getvalue())

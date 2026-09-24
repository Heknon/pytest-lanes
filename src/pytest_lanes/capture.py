"""Per-lane output capture: stdout/stderr and logging (touchpoints P3, P8).

pytest's own capture is process-wide, so lanes switch it off (``--capture=no``)
and capture per lane instead:

* ``sys.stdout``/``sys.stderr`` become ``_LaneStream`` objects that write to the
  current lane's buffers (bytes written to their ``.buffer`` included). The
  runner turns those buffers into report sections after each phase, as pytest's
  CaptureManager would. With ``-s``/``--capture=no`` they are not installed, and
  output goes straight to the terminal, as under xdist.
* ``contextlib.redirect_stdout``/``redirect_stderr`` entered on a lane redirect that
  lane's output only (P13): the target goes on the lane's own stack, which its
  ``_LaneStream`` writes to, instead of replacing ``sys.stdout`` for every lane.
  Swapping it for the process captured every other lane's prints too.
* ``sys.stdin`` becomes ``_NoStdin``, which fails a read at once, as pytest's
  capture does. Without it, a lane reading stdin blocked on the terminal forever.
  Left alone with ``-s``, as by pytest.
* The logging plugin's ``caplog_handler`` and ``report_handler`` become
  ``_LogDispatch`` stand-ins that forward to the current lane's own copies.
  A permanent ``_LogRouter`` on the root logger does the actual emitting, so
  pytest attaching and detaching handlers concurrently from many lanes is harmless.

On the main thread (no lane), everything falls through to the real objects.

P8: pytest >= 9 iterates ``logging.Logger.manager.loggerDict`` each time a test
phase starts. Another lane creating a logger at that moment made the iteration
raise "dictionary changed size", so the dict's views are served from a copy.
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
        self._binary = _LaneBinaryStream(self)

    def write(self, s):
        lane = LANE.get()
        if lane is None:
            return self._real.write(s)
        redirects = lane.redirects[self._attr]
        if redirects:
            return redirects[-1].write(s)
        return getattr(lane, self._attr).write(s)

    def flush(self):
        lane = LANE.get()
        if lane is not None and lane.redirects[self._attr]:
            return lane.redirects[self._attr][-1].flush()
        self._real.flush()

    @property
    def buffer(self):
        return self._binary

    def __getattr__(self, name):
        return getattr(self._real, name)


class _LaneBinaryStream:
    """``sys.stdout.buffer`` under lanes: bytes are decoded into the lane's text buffer."""

    def __init__(self, text: _LaneStream) -> None:
        self._text = text

    def write(self, b):
        lane = LANE.get()
        if lane is None:
            return self._text._real.buffer.write(b)
        encoding = getattr(self._text._real, "encoding", None) or "utf-8"
        redirects = lane.redirects[self._text._attr]
        if redirects:
            target = redirects[-1]
            if hasattr(target, "buffer"):
                return target.buffer.write(b)
            target.write(bytes(b).decode(getattr(target, "encoding", None) or encoding, "replace"))
            return len(b)
        getattr(lane, self._text._attr).write(bytes(b).decode(encoding, "replace"))
        return len(b)

    def flush(self):
        self._text.flush()

    def __getattr__(self, name):
        return getattr(self._text._real.buffer, name)


class _NoStdin(io.TextIOBase):
    """sys.stdin while capturing: reads fail, with pytest's own message."""

    encoding = "utf-8"
    MESSAGE = "pytest: reading from stdin while output is captured!  Consider using `-s`."

    def read(self, size=-1):
        raise OSError(self.MESSAGE)

    readline = read

    def readlines(self, hint=-1):
        raise OSError(self.MESSAGE)

    def __iter__(self):
        return self

    def __next__(self):
        raise OSError(self.MESSAGE)

    def fileno(self):
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no fileno()")

    def isatty(self):
        return False

    def readable(self):
        return False

    def close(self):
        pass

    @property
    def buffer(self):
        return self


# ------------------------------------------------------------------ P13: redirect_stdout
_REDIRECTED = {"stdout": "out", "stderr": "err"}


def check_p13():
    """Probe: contextlib's redirect classes still look as ``per_lane_redirects`` expects."""
    base = getattr(contextlib, "_RedirectStream", None)
    if base is None or not {"__enter__", "__exit__"} <= set(vars(base)):
        return "P13 contextlib._RedirectStream.__enter__/__exit__"
    if (getattr(contextlib.redirect_stdout, "_stream", None), getattr(contextlib.redirect_stderr, "_stream", None)) \
            != ("stdout", "stderr"):
        return "P13 contextlib.redirect_stdout/redirect_stderr._stream"
    probe = contextlib.redirect_stdout(None)
    if not hasattr(probe, "_new_target"):
        return "P13 contextlib.redirect_stdout()._new_target"
    return None


@contextlib.contextmanager
def per_lane_redirects():
    """P13: redirect_stdout/redirect_stderr on a lane redirect that lane only."""
    base = contextlib._RedirectStream
    enter, exit_ = base.__dict__["__enter__"], base.__dict__["__exit__"]

    def __enter__(self):
        lane = LANE.get()
        attr = _REDIRECTED.get(self._stream)
        per_lane = lane is not None and attr is not None and isinstance(getattr(sys, self._stream), _LaneStream)
        self.__dict__.setdefault("_lanes_entered", []).append(lane if per_lane else None)
        if not per_lane:
            return enter(self)
        lane.redirects[attr].append(self._new_target)
        return self._new_target

    def __exit__(self, *exc):
        lane = self.__dict__["_lanes_entered"].pop()
        if lane is None:
            return exit_(self, *exc)
        lane.redirects[_REDIRECTED[self._stream]].pop()
        return None

    base.__enter__, base.__exit__ = __enter__, __exit__
    try:
        yield
    finally:
        base.__enter__, base.__exit__ = enter, exit_


@contextlib.contextmanager
def per_lane_std_streams():
    real_in, real_out, real_err = sys.stdin, sys.stdout, sys.stderr
    sys.stdin = _NoStdin()
    sys.stdout = _LaneStream(real_out, "out")
    sys.stderr = _LaneStream(real_err, "err")
    try:
        with per_lane_redirects():
            yield
    finally:
        sys.stdin, sys.stdout, sys.stderr = real_in, real_out, real_err


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


class _SnapshotLoggerDict(dict):
    """logging's registry of loggers, whose views come from an atomic copy.

    ``__iter__`` must stay dict's own: ``dict.copy`` takes its C fast path only
    while it is, and otherwise calls ``keys()`` (recursing into this class).
    """

    def keys(self):
        return dict.copy(self).keys()

    def values(self):
        return dict.copy(self).values()

    def items(self):
        return dict.copy(self).items()


@contextlib.contextmanager
def snapshot_logger_dict():
    """P8. Restores the original dict object, with any loggers created meanwhile."""
    manager = logging.Logger.manager
    original = manager.loggerDict
    manager.loggerDict = _SnapshotLoggerDict(original)
    try:
        yield
    finally:
        original.update(manager.loggerDict)
        manager.loggerDict = original


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

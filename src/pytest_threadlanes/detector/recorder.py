"""Live record of process-wide changes made *while* a test runs.

Snapshots are taken between phases, so a patch applied and undone inside one test
body (``with mock.patch(...)``, ``os.environ[k] = v`` then ``del``) is invisible
to them. Under lanes it is still visible to every other lane while it lasts. The
recorder catches these as they happen:

* ``unittest.mock``: ``patch``, ``patch.object``, ``patch.multiple`` and
  ``patch.dict``, and therefore pytest-mock (touchpoint D1: the private
  ``_patch.__enter__`` and ``_patch_dict._patch_dict``/``_unpatch_dict``, probed
  by ``check_d1``). ``patch.dict(os.environ, ...)`` is recorded by its keys (or
  ``env:*`` with ``clear=True``), because it rewrites every variable;
* pytest's ``MonkeyPatch`` (public API): ``setattr``, ``delattr``, ``setitem``,
  ``delitem``, ``setenv``, ``delenv``, ``chdir`` and ``syspath_prepend``;
* Python audit events ``os.putenv``, ``os.unsetenv`` and ``os.chdir`` (public),
  which cover direct ``os.environ`` writes and ``os.chdir``;
* the stdlib's process-wide setters (``SETTERS``: ``random.seed``,
  ``socket.setdefaulttimeout``, ``locale.setlocale``, ``os.umask``, ...), called
  from anywhere but pytest's own machinery. Each breaks concurrent tests;
* ``sys.stdout``/``stderr``/``stdin`` replaced during a test (click's CliRunner, a
  direct assignment), sampled every ``STDIO_INTERVAL`` seconds and at every audit
  event (opening a file, importing, starting a subprocess...), so a swap shorter
  than a poll is seen if the test does anything audited meanwhile. pytest's own capture
  objects and ``contextlib.redirect_*`` targets are not reported: under lanes the
  redirect is per lane (capture.py, P13).

Everything is installed for the session and restored at its end, except the audit
hook, which Python cannot remove: it is disabled instead.
"""
from __future__ import annotations

import contextlib
import importlib
import os
import sys
import threading
import types

import pytest

from ..isolation import _patch_by_path, _shared
from .sources import IGNORED_ENV

_NOTSET = object()

#: Process-wide setters: (module, function). A call changes state every lane shares.
SETTERS = (("random", "seed"), ("random", "setstate"), ("socket", "setdefaulttimeout"),
           ("locale", "setlocale"), ("os", "umask"), ("time", "tzset"),
           ("sys", "setrecursionlimit"), ("sys", "setswitchinterval"), ("logging", "disable"),
           ("gc", "disable"), ("gc", "set_threshold"), ("gc", "freeze"), ("signal", "signal"))
#: Callers whose setter calls are pytest's own business, not the test's.
MACHINERY = ("_pytest", "pytest", "pluggy", "xdist", "pytest_threadlanes", "pytest_timeout",
             "coverage", "pytest_cov", "execnet")
STDIO = ("stdout", "stderr", "stdin")
STDIO_INTERVAL = 0.001


def check_d1() -> str | None:
    """Probe for touchpoint D1; a problem description, or None."""
    from unittest import mock

    patch_cls = getattr(mock, "_patch", None)
    dict_cls = getattr(mock, "_patch_dict", None)
    if patch_cls is None or not hasattr(patch_cls, "__enter__"):
        return "D1 unittest.mock._patch.__enter__"
    if dict_cls is None or not all(callable(getattr(dict_cls, m, None))
                                   for m in ("_patch_dict", "_unpatch_dict")):
        return "D1 unittest.mock._patch_dict._patch_dict/_unpatch_dict"
    try:
        probe = patch_cls.__new__(patch_cls)
        probe.__init__(lambda: os, "sep", mock.DEFAULT, None, False, None, None, None, {})
    except Exception as e:  # the constructor signature changed
        return f"D1 unittest.mock._patch.__init__ ({type(e).__name__}: {e})"
    if not (callable(getattr(probe, "getter", None)) and getattr(probe, "attribute", None) == "sep"):
        return "D1 unittest.mock._patch.getter/.attribute"
    return None


def _name(obj) -> str:
    name = getattr(obj, "__name__", None)
    if isinstance(name, str):
        return name
    return type(obj).__qualname__


def _global_name(obj):
    """``module.name`` of a module global holding ``obj``, or None."""
    for mod_name, module in sorted(list(sys.modules.items()), key=lambda kv: kv[0]):
        try:
            items = list(vars(module).items())
        except Exception:             # a proxy in sys.modules whose __dict__ raises
            continue
        for name, value in items:
            if value is obj:
                return f"{mod_name}.{name}"
    return None


def _label(target, attribute=None, paths=None):
    """The recorded name of a shared patch target, or None for a test's own object.

    The path the snapshot walk reached the object at, when it did (``paths``: id ->
    path, from the test's first snapshot), so a patch and an assignment of the same
    thing share one name and one ``lanes_detect_ignore`` pattern: ``module:pkg.mod.attr``,
    ``module:pkg.mod.HOLDER.client.timeout``. Otherwise the run-time guard's judgement
    (P14): modules, classes and instances a module holds are shared, anything else is
    presumed the test's own.
    """
    t = type(target)
    if issubclass(t, types.ModuleType):
        base = f"module:{target.__name__}"
    elif paths and id(target) in paths:
        base = paths[id(target)]
    elif issubclass(t, type):
        if not _shared(target):
            return None
        base = f"module:{vars(target).get('__module__', '?')}.{target.__qualname__}"
    else:
        name = _global_name(target)
        if name is None:
            return None
        base = f"module:{name}"
    return base if attribute is None else f"{base}.{attribute}"


def _env_key(key) -> str:
    return key.decode(errors="replace") if isinstance(key, bytes) else str(key)


class Recorder:
    def __init__(self) -> None:
        self._active = False
        self._owner = None          # the thread running the test being recorded
        self.targets: set = set()
        self._lock = threading.Lock()
        self._muted = 0             # inside patch.dict(os.environ): it rewrites every key
        self._poller = None
        self._poller_stop = threading.Event()
        self._redirect_targets: list = []    # active contextlib.redirect_* targets
        self._baseline: dict = {}
        self.paths: dict = {}         # id -> walked path, from the test's first snapshot

    def add(self, target: str) -> None:
        if self._active and threading.current_thread() is self._owner:
            with self._lock:
                self.targets.add(target)

    def take(self, targets) -> set:
        """Remove ``targets`` from this test's record and return them (a fixture's)."""
        with self._lock:
            self.targets -= targets
        return set(targets)

    def muted(self, fn, *args):
        self._muted += 1
        try:
            return fn(*args)
        finally:
            self._muted -= 1

    # ---- per test ---------------------------------------------------------------
    def start(self) -> None:
        self.targets = set()
        self._owner = threading.current_thread()
        self._active = True
        self._start_poller()

    def stop(self) -> set:
        self._active = False
        if self._poller is not None:
            self._poller_stop.set()
            self._poller.join()
            self._poller = None
        return self.targets

    # ---- stdio replacement, sampled while a test runs ---------------------------------
    def _start_poller(self) -> None:
        self._baseline = {name: getattr(sys, name, None) for name in STDIO}
        self._poller_stop = threading.Event()

        def poll():
            while not self._poller_stop.wait(STDIO_INTERVAL):
                self.check_stdio()

        self._poller = threading.Thread(target=poll, name="lanes-detect-stdio", daemon=True)
        self._poller.start()

    def check_stdio(self) -> None:
        """Record a replaced sys.stdout/stderr/stdin. Called by the poller and on every
        audit event (``installed``), which catches swaps shorter than a poll."""
        baseline = self._baseline
        for name in STDIO:
            current = getattr(sys, name, None)      # (a test may delete sys.stdin)
            if current is baseline[name] or any(current is t for t in self._redirect_targets):
                continue
            if (type(current).__module__ or "").startswith("_pytest"):
                continue                    # pytest's own capture, suspended and resumed
            with self._lock:
                self.targets.add(f"sys.{name}")

    # ---- session-long install -----------------------------------------------------
    @contextlib.contextmanager
    def installed(self):
        from unittest import mock

        rec = self
        with contextlib.ExitStack() as stack:
            def patch_attr(owner, name, make):
                original = owner.__dict__[name]
                setattr(owner, name, make(original))
                stack.callback(setattr, owner, name, original)

            def mock_enter(original):
                def __enter__(self):
                    try:
                        label = _label(self.getter(), self.attribute, rec.paths)
                        if label is None and _patch_by_path(self):
                            label = f"module:{_name(self.getter())}.{self.attribute}"
                    except Exception:
                        label = f"?.{getattr(self, 'attribute', '?')}"
                    if label is not None:
                        rec.add(label)
                    return original(self)
                return __enter__

            def environ(target) -> bool:
                return target is os.environ or target == "os.environ"

            def mock_patch_dict(original):
                def _patch_dict(self):
                    target = self.in_dict
                    if environ(target):
                        # Name the patched keys: the patch rewrites the whole environment.
                        if self.clear:
                            rec.add("env:*")
                        for key in self.values:
                            rec.add(f"env:{key}")
                        return rec.muted(original, self)
                    label = f"module:{target}" if isinstance(target, str) else _label(target, None, rec.paths)
                    if label is not None:
                        rec.add(label)
                    return original(self)
                return _patch_dict

            def mock_unpatch_dict(original):
                def _unpatch_dict(self):
                    if environ(self.in_dict):
                        return rec.muted(original, self)
                    return original(self)
                return _unpatch_dict

            patch_attr(mock._patch, "__enter__", mock_enter)
            patch_attr(mock._patch_dict, "_patch_dict", mock_patch_dict)
            patch_attr(mock._patch_dict, "_unpatch_dict", mock_unpatch_dict)

            mp = pytest.MonkeyPatch

            def mp_setattr(original):
                def setattr_(self, target, name=_NOTSET, value=_NOTSET, raising=True):
                    if isinstance(target, str) and value is _NOTSET:
                        rec.add(f"module:{target}")
                    else:
                        label = _label(target, name, rec.paths)
                        if label is not None:
                            rec.add(label)
                    args = [a for a in (name, value) if a is not _NOTSET]
                    return original(self, target, *args, raising=raising)
                return setattr_

            def mp_delattr(original):
                def delattr_(self, target, name=_NOTSET, raising=True):
                    label = f"module:{target}" if isinstance(target, str) and name is _NOTSET \
                        else _label(target, name, rec.paths)
                    if label is not None:
                        rec.add(label)
                    args = [] if name is _NOTSET else [name]
                    return original(self, target, *args, raising=raising)
                return delattr_

            def mp_item(original):
                def item(self, dic, name, *args, **kwargs):
                    label = "sys.modules" if dic is sys.modules else _label(dic, None, rec.paths)
                    if dic is not os.environ and label is not None:
                        rec.add(f"{label}[{name!r}]")
                    return original(self, dic, name, *args, **kwargs)
                return item

            def mp_simple(label):
                def wrap(original):
                    def method(self, *args, **kwargs):
                        rec.add(label)
                        return original(self, *args, **kwargs)
                    return method
                return wrap

            patch_attr(mp, "setattr", mp_setattr)
            patch_attr(mp, "delattr", mp_delattr)
            patch_attr(mp, "setitem", mp_item)
            patch_attr(mp, "delitem", mp_item)
            patch_attr(mp, "syspath_prepend", mp_simple("sys.path"))
            # setenv/delenv/chdir reach os.putenv/unsetenv/chdir: the audit hook names them.

            def audit(event, args):
                # builtins.id: the walk calls id() for every object. check_stdio must not.
                if not rec._active or event == "builtins.id":
                    return
                rec.check_stdio()
                if rec._muted:
                    return
                if event in ("os.putenv", "os.unsetenv"):
                    key = _env_key(args[0])
                    if key not in IGNORED_ENV:
                        rec.add(f"env:{key}")
                elif event == "os.chdir":
                    rec.add("cwd")

            state = {"on": True}

            def gated(event, args):
                if state["on"]:
                    audit(event, args)

            def setter(module, name):
                def wrap(original):
                    label = f"{module}.{name}()"

                    def call(*args, **kwargs):
                        caller = sys._getframe(1).f_globals.get("__name__", "")
                        if caller.partition(".")[0] not in MACHINERY and not (
                                name == "setlocale" and (len(args) < 2 or args[1] is None)
                                and kwargs.get("locale") is None):
                            rec.add(label)
                        return original(*args, **kwargs)
                    return call
                return wrap

            for module, name in SETTERS:
                owner = importlib.import_module(module)
                if name in vars(owner):
                    patch_attr(owner, name, setter(module, name))

            def redirect_enter(original):
                def __enter__(self):
                    rec._redirect_targets.append(self._new_target)
                    return original(self)
                return __enter__

            def redirect_exit(original):
                def __exit__(self, *exc):
                    try:
                        return original(self, *exc)
                    finally:
                        targets = rec._redirect_targets
                        for i in range(len(targets) - 1, -1, -1):
                            if targets[i] is self._new_target:
                                del targets[i]
                                break
                return __exit__

            redirect = contextlib._RedirectStream
            patch_attr(redirect, "__enter__", redirect_enter)
            patch_attr(redirect, "__exit__", redirect_exit)

            sys.addaudithook(gated)
            stack.callback(state.update, on=False)   # audit hooks cannot be removed
            yield self

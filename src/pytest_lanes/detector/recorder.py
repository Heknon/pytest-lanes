"""Live record of process-wide changes made *while* a test runs.

Snapshots are taken between phases, so a patch applied and undone inside one test
body (``with mock.patch(...)``, ``os.environ[k] = v`` then ``del``) is invisible
to them. Under lanes it is still visible to every other lane while it lasts. The
recorder catches these as they happen:

* ``unittest.mock``: ``patch``, ``patch.object``, ``patch.multiple`` and
  ``patch.dict``, and therefore pytest-mock (touchpoint D1: the private
  ``_patch.__enter__`` and ``_patch_dict._patch_dict``, probed by ``check_d1``);
* pytest's ``MonkeyPatch`` (public API): ``setattr``, ``delattr``, ``setitem``,
  ``delitem``, ``setenv``, ``delenv``, ``chdir`` and ``syspath_prepend``;
* Python audit events ``os.putenv``, ``os.unsetenv`` and ``os.chdir`` (public),
  which cover direct ``os.environ`` writes and ``os.chdir``.

Everything is installed for the session and restored at its end, except the audit
hook, which Python cannot remove: it is disabled instead.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading

import pytest

from .sources import IGNORED_ENV

_NOTSET = object()


def check_d1() -> str | None:
    """Probe for touchpoint D1; a problem description, or None."""
    from unittest import mock

    patch_cls = getattr(mock, "_patch", None)
    dict_cls = getattr(mock, "_patch_dict", None)
    if patch_cls is None or not hasattr(patch_cls, "__enter__"):
        return "D1 unittest.mock._patch.__enter__"
    if dict_cls is None or not callable(getattr(dict_cls, "_patch_dict", None)):
        return "D1 unittest.mock._patch_dict._patch_dict"
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


def _env_key(key) -> str:
    return key.decode(errors="replace") if isinstance(key, bytes) else str(key)


class Recorder:
    def __init__(self) -> None:
        self._active = False
        self._owner = None          # the thread running the test being recorded
        self.targets: set = set()
        self._lock = threading.Lock()

    def add(self, target: str) -> None:
        if self._active and threading.current_thread() is self._owner:
            with self._lock:
                self.targets.add(target)

    # ---- per test ---------------------------------------------------------------
    def start(self) -> None:
        self.targets = set()
        self._owner = threading.current_thread()
        self._active = True

    def stop(self) -> set:
        self._active = False
        return self.targets

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
                        rec.add(f"{_name(self.getter())}.{self.attribute}")
                    except Exception:
                        rec.add(f"?.{getattr(self, 'attribute', '?')}")
                    return original(self)
                return __enter__

            def mock_patch_dict(original):
                def _patch_dict(self):
                    target = self.in_dict
                    if target is not os.environ:   # os.environ keys come from the audit hook
                        rec.add(f"patch.dict({target if isinstance(target, str) else _name(target)})")
                    return original(self)
                return _patch_dict

            patch_attr(mock._patch, "__enter__", mock_enter)
            patch_attr(mock._patch_dict, "_patch_dict", mock_patch_dict)

            mp = pytest.MonkeyPatch

            def mp_setattr(original):
                def setattr_(self, target, name=_NOTSET, value=_NOTSET, raising=True):
                    if isinstance(target, str) and value is _NOTSET:
                        rec.add(target)
                    else:
                        rec.add(f"{_name(target)}.{name}")
                    args = [a for a in (name, value) if a is not _NOTSET]
                    return original(self, target, *args, raising=raising)
                return setattr_

            def mp_delattr(original):
                def delattr_(self, target, name=_NOTSET, raising=True):
                    rec.add(target if isinstance(target, str) and name is _NOTSET
                            else f"{_name(target)}.{name}")
                    args = [] if name is _NOTSET else [name]
                    return original(self, target, *args, raising=raising)
                return delattr_

            def mp_item(original):
                def item(self, dic, name, *args, **kwargs):
                    if dic is not os.environ:
                        rec.add(f"{_name(dic)}[{name!r}]")
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
                if not rec._active:
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

            sys.addaudithook(gated)
            stack.callback(state.update, on=False)   # audit hooks cannot be removed
            yield self

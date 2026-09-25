"""What a snapshot covers: process-wide state, your modules, and plugin objects.

* **Process state:** environment variables, the working directory, ``sys.path``,
  logging levels, and signal handlers.
* **Your modules:** every imported module whose file is under the rootdir (outside
  any site-packages), plus the packages named in the ``lanes_detect_modules`` ini.
  The walk covers their globals and the class attributes of the classes they define.
* **Plugin objects:** every object registered with pytest's plugin manager, except
  pytest's, pluggy's, xdist's and pytest-threadlanes' own, whose per-test state lanes
  already keep per lane. A plugin instance created in ``pytest_configure`` is found
  here even if no module global refers to it.

Objects are expanded (their attributes walked) when their type comes from one of
these packages; see walk.py.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import types
from pathlib import Path

from .walk import Walker

#: Owned by pytest and pytest-threadlanes, and kept per lane already (touchpoint P6).
IGNORED_ENV = frozenset({"PYTEST_CURRENT_TEST"})
#: Plugin packages whose state lanes already isolate or that are process machinery.
_CORE = ("_pytest", "pytest", "pluggy", "xdist", "pytest_threadlanes", "py", "execnet")


def _top(name: str) -> str:
    return name.partition(".")[0]


def _is_core(module_name: str) -> bool:
    return _top(module_name or "") in _CORE


class Sources:
    def __init__(self, config, *, max_depth: int, max_nodes: int) -> None:
        self.config = config
        self.rootdir = Path(str(config.rootpath)).resolve()
        self.extra = tuple(config.getini("lanes_detect_modules"))
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.truncated = False

    # ---- scope ----------------------------------------------------------------
    def _under_rootdir(self, module) -> bool:
        file = getattr(module, "__file__", None)
        if not file:
            return False
        try:
            path = Path(file).resolve()
        except (OSError, ValueError):
            return False
        return (self.rootdir in path.parents and "site-packages" not in path.parts
                and "dist-packages" not in path.parts)

    def _named(self, name: str) -> bool:
        return any(name == p or name.startswith(p + ".") for p in self.extra)

    def modules(self) -> list:
        found = []
        for name, module in sorted(list(sys.modules.items()), key=lambda kv: kv[0]):
            if not issubclass(type(module), types.ModuleType) or _is_core(name):
                continue
            if self._named(name) or self._under_rootdir(module):
                found.append((name, module))
        return found

    def plugins(self) -> list:
        pm = self.config.pluginmanager
        found = []
        for name, plugin in sorted(((n or "", p) for n, p in pm.list_name_plugin() if p is not None),
                                   key=lambda np: np[0]):
            if issubclass(type(plugin), types.ModuleType) or _is_core(type(plugin).__module__):
                continue
            found.append((name or type(plugin).__qualname__, plugin))
        return found

    # ---- one snapshot ---------------------------------------------------------
    def snapshot(self) -> dict:
        modules = self.modules()
        plugins = self.plugins()
        packages = {_top(name) for name, _ in modules}
        packages |= {_top(type(p).__module__) for _, p in plugins}
        packages |= {_top(p) for p in self.extra}
        packages -= set(_CORE)

        def walkable(cls) -> bool:
            return _top(getattr(cls, "__module__", "") or "") in packages

        walker = Walker(walkable, max_depth=self.max_depth, max_nodes=self.max_nodes)
        self._process_state(walker.state)
        for name, plugin in plugins:
            walker.visit(f"plugin:{name}", plugin)
        for name, module in modules:
            self._walk_module(walker, name, module)
        self.truncated = self.truncated or walker.truncated
        return walker.snapshot()

    def _walk_module(self, walker: Walker, name: str, module) -> None:
        try:
            items = list(vars(module).items())
        except TypeError:
            return
        for attr, value in items:
            if attr.startswith("__") and attr.endswith("__"):
                continue
            path = f"module:{name}.{attr}"
            if issubclass(type(value), type) and vars(value).get("__module__") != name:
                walker.state[path] = ("class", id(value))   # walked where it is defined
                continue
            walker.visit(path, value)

    @staticmethod
    def _process_state(state: dict) -> None:
        for key, value in list(os.environ.items()):
            if key not in IGNORED_ENV:
                state[f"env:{key}"] = ("str", len(value), hash(value))
        try:
            state["cwd"] = ("str", os.getcwd())
        except OSError:
            state["cwd"] = ("str", None)
        state["sys.path"] = ("path", tuple(p if isinstance(p, str) else repr(p) for p in sys.path))
        root = logging.getLogger()
        state["logging:root.level"] = ("int", root.level)
        for name, logger in list(logging.Logger.manager.loggerDict.items()):
            if isinstance(logger, logging.Logger):
                state[f"logging:{name}.level"] = ("int", logger.level)
                state[f"logging:{name}.disabled"] = ("bool", logger.disabled)
        for sig in sorted(signal.valid_signals()):
            try:
                handler = signal.getsignal(sig)
            except (ValueError, OSError):
                continue
            name = getattr(sig, "name", str(sig))
            state[f"signal:{name}"] = ("handler", id(handler) if callable(handler) else repr(handler))

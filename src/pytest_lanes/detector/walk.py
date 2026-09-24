"""Recursive snapshot of object state: ``{path: fingerprint}``.

A fingerprint is a small tuple of plain values, so comparing two snapshots never
runs user code. The walk is read-only and never runs user code either:

* attributes come from ``__dict__`` and ``__slots__`` directly, so properties,
  ``__getattr__`` and descriptors are never evaluated;
* strings are fingerprinted by length and hash, so values (tokens, passwords in
  environment variables) are not kept;
* objects are identified by ``id()``, so no ``__eq__`` or ``__hash__`` is called.

It follows dicts, lists, tuples, sets, class attributes and the attributes of
objects whose type is *walkable* (defined in a package being inspected), to
``max_depth`` levels, and stops everywhere else at the object's identity. Each
object is expanded once per snapshot, at the first path that reaches it, which
also makes cycles safe.
"""
from __future__ import annotations

import collections
import types

_SCALARS = (type(None), bool, int, float, complex)
_TEXT = (str, bytes)
_SEQUENCES = (list, tuple, collections.deque)
_SETS = (set, frozenset)
_CALLABLES = (types.FunctionType, types.BuiltinFunctionType, types.MethodType,
              staticmethod, classmethod, property, types.MemberDescriptorType,
              types.GetSetDescriptorType, types.WrapperDescriptorType,
              types.MethodDescriptorType, types.CodeType)

#: Items followed per container; larger containers are still fingerprinted by length.
MAX_ITEMS = 1000


def _label(key) -> str:
    if isinstance(key, (_TEXT, _SCALARS)):
        text = repr(key)
        return text if len(text) <= 80 else text[:77] + "..."
    return f"<{type(key).__name__}>"


def _base_iter(obj):
    for base in _SEQUENCES:
        if isinstance(obj, base):
            return base.__iter__(obj)
    return iter(())


def _attributes(obj):
    """(name, value) from __dict__ and __slots__, without evaluating any descriptor."""
    try:
        d = object.__getattribute__(obj, "__dict__")
    except Exception:
        d = None
    if isinstance(d, dict):
        yield from list(d.items())
    for cls in type(obj).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        for name in (slots,) if isinstance(slots, str) else slots:
            member = cls.__dict__.get(name)
            if isinstance(member, types.MemberDescriptorType):
                try:
                    yield name, member.__get__(obj, cls)
                except AttributeError:        # slot never assigned
                    pass


class Walker:
    def __init__(self, walkable, *, max_depth: int, max_nodes: int) -> None:
        self.walkable = walkable          # type -> bool: expand this type's instances?
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.state: dict = {}
        self._seen: set = set()
        self.truncated = False

    def visit(self, path: str, obj, depth: int = 0) -> None:
        if len(self.state) >= self.max_nodes:
            self.truncated = True
            return
        state = self.state
        if isinstance(obj, _SCALARS):   # floats by repr: nan != nan would read as a change
            state[path] = (type(obj).__name__, repr(obj) if isinstance(obj, (float, complex)) else obj)
            return
        if isinstance(obj, _TEXT):
            state[path] = (type(obj).__name__, len(obj), hash(obj))
            return
        if isinstance(obj, types.ModuleType):
            state[path] = ("module", obj.__name__)
            return
        if isinstance(obj, _CALLABLES):
            state[path] = ("callable", id(obj))
            return
        ident = id(obj)
        if ident in self._seen or depth >= self.max_depth:
            state[path] = ("ref", type(obj).__name__, ident)
            return
        self._seen.add(ident)
        if isinstance(obj, dict):
            items = list(dict.items(obj))     # the base methods: a subclass's overrides never run
            state[path] = ("dict", len(items))
            for key, value in items[:MAX_ITEMS]:
                self.visit(f"{path}[{_label(key)}]", value, depth + 1)
        elif isinstance(obj, _SEQUENCES):
            items = list(obj) if type(obj) in _SEQUENCES else list(_base_iter(obj))
            state[path] = ("seq", len(items))
            for i, value in enumerate(items[:MAX_ITEMS]):
                self.visit(f"{path}[{i}]", value, depth + 1)
        elif isinstance(obj, _SETS):
            scalars = frozenset(v for v in obj if isinstance(v, (_SCALARS, _TEXT)))
            state[path] = ("set", len(obj), hash(scalars))
        elif isinstance(obj, type):
            state[path] = ("class", ident)
            if self.walkable(obj):
                self.walk_class(path, obj, depth)
        else:
            state[path] = ("object", type(obj).__qualname__, ident)
            if self.walkable(type(obj)) or isinstance(obj, types.SimpleNamespace):
                for name, value in _attributes(obj):
                    self.visit(f"{path}.{name}", value, depth + 1)

    def walk_class(self, path: str, cls: type, depth: int) -> None:
        """Class-level attributes: the ones code assigns to as ``Cls.attr = ...``."""
        for name, value in list(vars(cls).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(value, _CALLABLES):
                continue
            self.visit(f"{path}.{name}", value, depth + 1)

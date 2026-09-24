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
also makes cycles safe. Types are read with ``type()``, never ``isinstance()``:
that reads ``__class__``, which a proxy (a dead ``weakref.proxy``, werkzeug's
``LocalProxy``) raises from, and a spec'd mock fakes.
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


def _plain(key) -> bool:
    """A key whose repr is its identity: a scalar, text, or a tuple/frozenset of them."""
    t = type(key)
    if t in _SCALARS or t in _TEXT:
        return True
    if t is tuple or t is frozenset:
        return all(_plain(k) for k in key)
    return False


def _label(key) -> str:
    if _plain(key):
        text = repr(key)
        return text if len(text) <= 80 else text[:77] + "..."
    return f"<{type(key).__name__}>"


def _attributes(obj):
    """(name, value) from __dict__ and __slots__, without evaluating any descriptor."""
    try:
        d = object.__getattribute__(obj, "__dict__")
    except Exception:
        d = None
    if type(d) is dict:
        yield from list(dict.items(d))
    for cls in type(obj).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        for name in (slots,) if type(slots) is str else slots:
            member = cls.__dict__.get(name)
            if type(member) is types.MemberDescriptorType:
                try:
                    yield name, member.__get__(obj, cls)
                except AttributeError:        # slot never assigned
                    pass


def _items(obj, kind):
    """The items of a container, through its base type's C methods only: a subclass's
    overrides never run, and the copy is atomic (a set another thread changes)."""
    if kind == "dict":
        return list(dict.items(obj))
    if kind is collections.deque:
        return list(collections.deque.copy(obj))
    if kind in _SEQUENCES:
        return list(kind.__iter__(obj))
    if issubclass(type(obj), set):
        return list(set.copy(obj))
    return list(frozenset.__iter__(obj))


def _kind(t):
    """The walk's category for a type, from type(obj): isinstance() would read
    ``__class__``, which proxies and spec'd mocks answer (or raise from)."""
    if issubclass(t, dict):
        return "dict"
    for base in _SEQUENCES:
        if issubclass(t, base):
            return base
    if issubclass(t, _SETS):
        return "set"
    if issubclass(t, type):
        return "class"
    return None


class Snapshot(dict):
    """``{path: fingerprint}``, with the walk's ``unexpanded`` paths and ``truncated`` flag."""

    unexpanded: frozenset = frozenset()
    truncated: bool = False
    paths: dict = {}          # id(object) -> the path it was expanded at

    def covers(self, path: str, ancestors) -> bool:
        """Would ``path`` be in this snapshot if it existed? No when the walk did not
        look there: the path or an ancestor was refused at the node cap, or was not
        expanded here (the object was expanded at another path)."""
        return path not in self.unexpanded and not any(a in self.unexpanded for a in ancestors(path))


def rebase(snapshot, old, new):
    """``snapshot`` with every path that differs between ``old`` and ``new`` taken from
    ``new`` (absent there: removed)."""
    result = Snapshot(snapshot)
    result.unexpanded, result.truncated = snapshot.unexpanded, snapshot.truncated
    result.paths = snapshot.paths
    for path in old.keys() | new.keys():
        if old.get(path) != new.get(path):
            if path in new:
                result[path] = new[path]
            else:
                result.pop(path, None)
    return result


class Walker:
    """``state`` maps paths to fingerprints. A container or walkable object has the same
    fingerprint wherever it is reached; its contents are listed under the first path
    that reaches it. ``unexpanded`` names the other paths (and those cut at
    ``max_depth``): their contents are not in ``state``, so a missing sub-path under
    one means nothing."""

    def __init__(self, walkable, *, max_depth: int, max_nodes: int) -> None:
        self.walkable = walkable          # type -> bool: expand this type's instances?
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.state: dict = {}
        self.unexpanded: set = set()
        self._seen: dict = {}             # id(object) -> the path it was expanded at
        self.truncated = False

    def snapshot(self) -> Snapshot:
        snap = Snapshot(self.state)
        snap.unexpanded = frozenset(self.unexpanded)
        snap.truncated = self.truncated
        snap.paths = self._seen
        return snap

    def visit(self, path: str, obj, depth: int = 0) -> None:
        if len(self.state) >= self.max_nodes:
            self.truncated = True
            self.unexpanded.add(path)     # not looked at: absence here means nothing
            return
        try:
            self._visit(path, obj, depth)
        except Exception as e:   # never let one odd object stop the run
            self.state[path] = ("unreadable", type(e).__name__, id(obj))

    def _visit(self, path: str, obj, depth: int) -> None:
        state = self.state
        t = type(obj)
        if t in _SCALARS:   # floats by repr: nan != nan would read as a change
            state[path] = (t.__name__, repr(obj) if t in (float, complex) else obj)
            return
        if t in _TEXT:
            state[path] = (t.__name__, len(obj), hash(obj))
            return
        if issubclass(t, types.ModuleType):
            state[path] = ("module", object.__getattribute__(obj, "__dict__").get("__name__"))
            return
        if issubclass(t, _CALLABLES):
            state[path] = ("callable", id(obj))
            return
        ident = id(obj)
        kind = _kind(t)
        if kind is None:
            expand = self.walkable(t) or t is types.SimpleNamespace
            state[path] = ("object", t.__qualname__, ident)
        elif kind == "class":
            expand = self.walkable(obj)
            state[path] = ("class", ident)
        else:
            items = _items(obj, kind)
            if kind == "set":
                scalars = frozenset(v for v in items if type(v) in _SCALARS or type(v) in _TEXT)
                state[path] = ("set", len(items), hash(scalars))
                return
            if kind == "dict":
                keys = frozenset(k if _plain(k) else ("id", id(k)) for k, _ in items)
                state[path] = ("dict", len(items), hash(keys), ident)
            else:
                state[path] = ("seq", len(items), ident)
            expand = True
        if not expand:
            return
        if ident in self._seen or depth >= self.max_depth:
            self.unexpanded.add(path)
            return
        self._seen[ident] = path
        if kind == "class":
            self.walk_class(path, obj, depth)
        elif kind is None:
            for name, value in _attributes(obj):
                self.visit(f"{path}.{name}", value, depth + 1)
        elif kind == "dict":
            for key, value in items[:MAX_ITEMS]:
                self.visit(f"{path}[{_label(key)}]", value, depth + 1)
        else:
            for i, value in enumerate(items[:MAX_ITEMS]):
                self.visit(f"{path}[{i}]", value, depth + 1)

    def walk_class(self, path: str, cls: type, depth: int) -> None:
        """Class-level attributes: the ones code assigns to as ``Cls.attr = ...``."""
        for name, value in list(vars(cls).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if issubclass(type(value), _CALLABLES):
                continue
            self.visit(f"{path}.{name}", value, depth + 1)

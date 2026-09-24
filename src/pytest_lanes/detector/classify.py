"""Turn per-test snapshot differences into findings.

Each test gets three snapshots: before setup (A), after its call phase (B, with
fixtures still active), and after teardown (C). A session, package, module or class
fixture's setup and teardown are snapshotted too: their changes are reported under
``fixture <name> (<scope> scope)``, not against the test that happened to run them.
For each path that differs:

* **temporary** in a test: A == C but A != B. The state was changed for the test
  and restored after. Under lanes, other tests see it meanwhile.
* **persisted** in a test: A != C.

A path's finding, by its history over the whole run:

========== ============================================ ==========
kind       history                                      severity
========== ============================================ ==========
per-test   temporary in any test, or persisted in 2+    unsafe
           (a "current test" field, a patched global)
patched    patched while a test ran (recorder.py)       unsafe
grows      a container that grew in 2+ tests            check
set-once   persisted in exactly one test, then stable   ok
           (a cache or lazy initialisation)
========== ============================================ ==========

A path inside one that is already reported with the same or a higher severity is
left out: the new object that a per-test field points to, or the entries of a
cache that was filled once. A module global that was patched is reported once, as
``patched``. A test that changes more than ``ENV_BULK`` environment variables
(``patch.dict(os.environ, clear=True)``) gives one ``env:*`` finding.
"""
from __future__ import annotations

import re
from collections import defaultdict
from fnmatch import fnmatchcase

SEVERITY = {"per-test": "unsafe", "patched": "unsafe", "grows": "check", "set-once": "ok"}
_RANK = {"unsafe": 2, "check": 1, "ok": 0}
_CONTAINERS = ("dict", "seq", "set")
EXAMPLES = 3
ENV_BULK = 20

NOTES = {
    "per-test": "changes while tests run, so concurrent tests see each other's value. "
                "Make it per lane (a contextvar), or mark the tests lanes_exclusive.",
    "patched": "patched while a test runs; under lanes the patch is visible to every test "
               "in the process. Mark the test lanes_exclusive, or patch something lane-local.",
    "grows": "grows with every test: check that it is thread-safe and that nothing reads its "
             "last entry as 'the current test'.",
    "set-once": "set once, then stable: fine if it is a cache that is safe to share between threads.",
}

_SPLIT = re.compile(r"[.\[]")


def ancestors(path: str):
    """Proper prefixes of a path at attribute and item boundaries."""
    head, sep, rest = path.partition(":")
    base = head + sep
    for m in _SPLIT.finditer(rest):
        if m.start():
            yield base + rest[:m.start()]


class Collector:
    def __init__(self, ignore) -> None:
        self.ignore = tuple(ignore)
        self.tests = 0
        self._temporary: dict = defaultdict(list)
        self._persisted: dict = defaultdict(list)   # path -> [(nodeid, before, after)]
        self._patched: dict = defaultdict(list)

    def ignored(self, path: str) -> bool:
        return any(fnmatchcase(p, pat) for p in (path, *ancestors(path)) for pat in self.ignore)

    def add_fixture(self, label: str, before: dict, after: dict, patched=()) -> None:
        """A wider-scoped fixture's setup or teardown changed ``before`` into ``after``."""
        self.add_test(label, before, after, after, patched, count=False)

    def add_test(self, nodeid: str, a: dict, b: dict, c: dict, patched, count: bool = True) -> None:
        if count:
            self.tests += 1
        changed = {k for k, _ in a.items() ^ b.items()} | {k for k, _ in a.items() ^ c.items()}
        env = {p for p in changed if p.startswith("env:")}
        if len(env) > ENV_BULK:
            changed -= env
            if any(a.get(p) != c.get(p) for p in env):
                self._persisted["env:*"].append((nodeid, ("env", 0), ("env", 1)))
            else:
                self._temporary["env:*"].append(nodeid)
        patched = set(patched)
        env = {p for p in patched if p.startswith("env:")}
        if len(env) > ENV_BULK:
            patched = (patched - env) | {"env:*"}
        for path in changed:
            # Absent from a snapshot that did not look there (node cap, or the object was
            # expanded at another path in it): not a change.
            if any(path not in s and not _covers(s, path) for s in (a, b, c)):
                continue
            before, during, after = a.get(path), b.get(path), c.get(path)
            if before != after:
                self._persisted[path].append((nodeid, before, after))
            elif before != during:
                self._temporary[path].append(nodeid)
        for target in patched:
            self._patched[target].append(nodeid)

    def findings(self) -> list:
        raw = {}
        for path in set(self._temporary) | set(self._persisted):
            temporary, persisted = self._temporary.get(path, []), self._persisted.get(path, [])
            nodeids = list(dict.fromkeys(temporary + [n for n, _, _ in persisted]))
            if temporary or len(persisted) >= 2:
                kind = "grows" if not temporary and _grew(persisted) else "per-test"
            else:
                kind = "set-once"
            raw[path] = (kind, nodeids)

        kept = []
        patched = set(self._patched)
        for path, (kind, nodeids) in raw.items():
            if self.ignored(path) or path in patched or (path.startswith("module:") and path[7:] in patched):
                continue
            rank = _RANK[SEVERITY[kind]]
            if any(p in raw and _RANK[SEVERITY[raw[p][0]]] >= rank for p in ancestors(path)):
                continue
            kept.append(_finding(kind, path, nodeids))
        for target, nodeids in self._patched.items():
            if not self.ignored(target):
                kept.append(_finding("patched", target, list(dict.fromkeys(nodeids))))
        kept.sort(key=lambda f: (-_RANK[f["severity"]], f["kind"], f["path"]))
        return kept


def _covers(snapshot, path: str) -> bool:
    covers = getattr(snapshot, "covers", None)
    return covers(path, ancestors) if covers else True


def _grew(persisted) -> bool:
    for _, before, after in persisted:
        if not (before and after and before[0] in _CONTAINERS and after[0] == before[0]
                and after[1] > before[1]):
            return False
    return True


def _finding(kind: str, path: str, nodeids: list) -> dict:
    return {"kind": kind, "severity": SEVERITY[kind], "path": path, "tests": len(nodeids),
            "examples": nodeids[:EXAMPLES], "note": NOTES[kind], "nodeids": nodeids}


FIXTURE = "fixture "


def unsafe_tests(findings: list, fixtures: bool = False) -> list:
    """Every test behind an unsafe finding: the candidates for lanes_exclusive. With
    ``fixtures``, the wider-scoped fixtures behind one instead."""
    return sorted({n for f in findings if f["severity"] == "unsafe" for n in f["nodeids"]
                   if n.startswith(FIXTURE) == fixtures})

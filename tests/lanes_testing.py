"""Helpers shared by the contract tests. Imported by the test modules (tests/ is on sys.path)."""
from __future__ import annotations

import json
import sys

import _pytest.threadexception as _te
import _pytest.unraisableexception as _ue

#: Flags every inner run needs. See CLAUDE.md "Required flags".
BASE = ["-p", "no:warnings", "-p", "no:cacheprovider", "-p", "no:randomly"]
if not hasattr(_te, "pytest_configure"):     # pytest <= 8.3.5 swaps global hooks per test
    BASE += ["-p", "no:threadexception"]
if not hasattr(_ue, "pytest_configure"):
    BASE += ["-p", "no:unraisableexception"]

#: The three execution modes that must be indistinguishable to consumers.
MODES = {
    "xdist": ["-n", "2"],
    "lanes": ["--lanes", "3"],
    "hybrid": ["-n", "2", "--lanes", "2"],
}
CONTEXT_AWARE_WARNINGS = sys.version_info >= (3, 14)


def run(pytester, *args, timeout=120):
    return pytester.runpytest_subprocess(*BASE, *args, timeout=timeout)


def dist_args(mode: list[str], dist: str) -> list[str]:
    """Choose a built-in scheduler the way each mode takes it."""
    return [*mode, "--dist", dist] if mode[0] == "-n" else [*mode, "--lanes-dist", dist]


def report_log(pytester, *args, timeout=120):
    """Run, and return (result, sorted TestReport rows) from pytest-reportlog.

    A row is (nodeid, when, outcome, section names): what the parity invariant compares.
    """
    path = pytester.path / "rl.jsonl"
    if path.exists():
        path.unlink()
    result = run(pytester, *args, f"--report-log={path}", timeout=timeout)
    rows = sorted((e["nodeid"], e["when"], e["outcome"], tuple(s[0] for s in e["sections"]))
                  for e in map(json.loads, open(path))
                  if e.get("$report_type") == "TestReport")
    return result, rows

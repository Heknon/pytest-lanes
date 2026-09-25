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

#: The user's scheduling pattern: one scope per environment, from a parametrize id
#: like 'test_x.py::test_step[envB-2]' -> 'envB'.
ENV_SCHED = """
from xdist.scheduler import LoadScopeScheduling

class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid):
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]

def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)
"""

#: The scheduler README.md recommends, verbatim: a lane gets its next environment only
#: when it has at most one test left (the one a worker holds back until it knows what
#: comes next). xdist's loadscope tops a node up at two, so an environment could wait
#: behind two tests of another while other lanes sat idle.
RECOMMENDED_SCHED = """
from xdist.scheduler import LoadScopeScheduling

class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid):     # 'test_x.py::test_step[envB-2]' -> 'envB'
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]

    def _reschedule(self, node):
        # Queue the next environment only behind the lane's last test, not its last two.
        if node.shutting_down or not self.workqueue or self._pending_of(self.assigned_work[node]) <= 1:
            super()._reschedule(node)

def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)
"""


def without(*plugins) -> list:
    """BASE without some of its ``-p no:X`` pairs: those plugins stay on."""
    return [a for pair in zip(BASE[::2], BASE[1::2]) if pair[1] not in {f"no:{p}" for p in plugins}
            for a in pair]


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


#: For test bodies: ``with stamped(request):`` records when the body ran (and in which
#: process) in the directory $LANES_STAMPS. Asserting overlap instead of wall-clock
#: duration keeps the timing tests stable under load.
STAMPED = '''
import contextlib as _cl, os as _os, time as _time
@_cl.contextmanager
def stamped(request):
    start = _time.monotonic()
    try:
        yield
    finally:
        name = request.node.nodeid.replace("/", "_").replace(":", "_")
        with open(_os.path.join(_os.environ["LANES_STAMPS"], name), "w") as f:
            f.write(f"{start} {_time.monotonic()} {_os.getpid()}")
'''


def stamps_dir(pytester, monkeypatch):
    """Where stamps go; also writes ``stamping.py`` (``from stamping import stamped``)."""
    d = pytester.path / "stamps"
    d.mkdir()
    (pytester.path / "stamping.py").write_text(STAMPED)
    monkeypatch.setenv("LANES_STAMPS", str(d))
    return d


def max_overlap(stamps, pid=None, match=None) -> int:
    """The largest number of stamped test bodies running at one moment (in one process;
    of the tests whose stamp name contains ``match``)."""
    events = []
    for f in stamps.iterdir():
        if match is not None and match not in f.name:
            continue
        start, end, owner = f.read_text().split()
        if pid is None or int(owner) == pid:
            events += [(float(start), 1), (float(end), -1)]
    running = best = 0
    for _, delta in sorted(events, key=lambda e: (e[0], e[1])):   # an end before a start at a tie
        running += delta
        best = max(best, running)
    return best


def stamp_pids(stamps) -> set:
    return {int(f.read_text().split()[2]) for f in stamps.iterdir()}

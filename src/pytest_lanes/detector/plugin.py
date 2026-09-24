"""``--lanes-detect``: the detector's pytest hooks.

Tests run one at a time, as in a plain pytest run. Around each test the detector
takes three snapshots (sources.py) and records live patches (recorder.py);
classify.py turns the differences into findings, and report.py prints them.
"""
from __future__ import annotations

import pytest

from .classify import NOTES, Collector, unsafe_tests
from .recorder import Recorder, check_d1
from .report import write_json, write_terminal
from .sources import Sources
from .walk import rebase

CONCURRENT = ("--lanes-detect runs tests one at a time, to see what each test changes; "
              "run it without --lanes and -n")


def refuse_concurrent(config) -> None:
    """UsageError when the detector is combined with lanes or xdist, or D1 is broken."""
    if (config.getoption("lanes") or getattr(config.option, "numprocesses", None)
            or getattr(config.option, "tx", None) or hasattr(config, "workerinput")):
        raise pytest.UsageError(CONCURRENT)
    problem = check_d1()
    if problem:
        raise pytest.UsageError(f"pytest-lanes: --lanes-detect cannot record patches on this "
                                f"Python: an internal changed:\n  {problem}")


class SharedStateDetector:
    def __init__(self, config) -> None:
        self.config = config
        self.sources = Sources(config, max_depth=int(config.getini("lanes_detect_max_depth")),
                               max_nodes=int(config.getini("lanes_detect_max_nodes")))
        self.collector = Collector(config.getini("lanes_detect_ignore"))
        self.recorder = Recorder()
        self._during: dict = {}
        self._setups: list = []      # (before, after) of wider-scoped fixture setups in this test
        self._teardowns: list = []   # (before, after) of their teardowns
        self._paths = 0
        self._installed = None

    def pytest_sessionstart(self, session):
        self._installed = self.recorder.installed()
        self._installed.__enter__()

    def pytest_sessionfinish(self, session):
        if self._installed is not None:
            self._installed.__exit__(None, None, None)
            self._installed = None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        before = self.sources.snapshot()
        self._during.pop(item.nodeid, None)
        self._setups, self._teardowns = [], []
        self.recorder.start()
        try:
            return (yield)
        finally:
            patched = self.recorder.stop()
            after = self.sources.snapshot()
            during = self._during.pop(item.nodeid, after)
            self._paths = max(self._paths, len(after))
            # What a wider-scoped fixture set up or tore down during this test is the
            # fixture's, not the test's (it was reported against the first and last test).
            for old, new in self._setups:
                before = rebase(before, old, new)
            for old, new in self._teardowns:
                after = rebase(after, new, old)
            self.collector.add_test(item.nodeid, before, during, after, patched)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        """Snapshots around a session/package/module/class fixture's setup and teardown."""
        if fixturedef.scope == "function" or not self.recorder._active:
            return (yield)
        label = f"fixture {fixturedef.argname} ({fixturedef.scope} scope)"
        before = self.sources.snapshot()
        patches = set(self.recorder.targets)
        state: dict = {}

        def after_teardown():
            end = self.sources.snapshot()
            self._teardowns.append((state["start"], end))
            self.collector.add_fixture(label, state["start"], end)

        def before_teardown():
            state["start"] = self.sources.snapshot()

        fixturedef.addfinalizer(after_teardown)     # finalizers run last-in, first-out:
        try:                                        # this one after the fixture's own
            return (yield)
        finally:
            after = self.sources.snapshot()
            self._setups.append((before, after))
            self.collector.add_fixture(label, before, after,
                                       self.recorder.take(self.recorder.targets - patches))
            fixturedef.addfinalizer(before_teardown)  # and this one before it

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        try:
            return (yield)
        finally:
            self._during[item.nodeid] = self.sources.snapshot()   # fixtures still active

    def report(self) -> dict:
        findings = self.collector.findings()
        return {"tests": self.collector.tests, "paths": self._paths,
                "truncated": self.sources.truncated, "findings": findings,
                "unsafe_tests": unsafe_tests(findings),
                "unsafe_fixtures": unsafe_tests(findings, fixtures=True), "notes": NOTES}

    def pytest_terminal_summary(self, terminalreporter):
        report = self.report()
        path = self.config.getoption("lanes_detect_report")
        write_terminal(terminalreporter, report, path)
        if path:
            write_json(path, report)

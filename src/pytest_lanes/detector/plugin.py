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
        self.recorder.start()
        try:
            return (yield)
        finally:
            patched = self.recorder.stop()
            after = self.sources.snapshot()
            during = self._during.pop(item.nodeid, after)
            self._paths = max(self._paths, len(after))
            self.collector.add_test(item.nodeid, before, during, after, patched)

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
                "unsafe_tests": unsafe_tests(findings), "notes": NOTES}

    def pytest_terminal_summary(self, terminalreporter):
        report = self.report()
        path = self.config.getoption("lanes_detect_report")
        write_terminal(terminalreporter, report, path)
        if path:
            write_json(path, report)

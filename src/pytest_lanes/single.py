"""Single-process mode (``--lanes N``): this process is both xdist controller and workers.

The main thread plays xdist's ``DSession``: ``add_node``, ``add_node_collection``,
``schedule``, ``mark_test_complete``, and shutdown once ``tests_finished``. The N
lanes play the workers. Exclusive tests (``lanes_exclusive``, capsys, ...) run
afterwards on one extra lane, ``ln-serial``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from .runner import LaneRunner
from .scheduling import add_group_suffix, make_scheduler


class SingleProcessSession(LaneRunner):
    set_report_node = True

    def __init__(self, config) -> None:
        super().__init__(config)
        self.sched = None

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        from xdist.scheduler import LoadGroupScheduling

        self.sched = make_scheduler(config, self.n)
        if isinstance(self.sched, LoadGroupScheduling):
            add_group_suffix(items)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        if session.testsfailed and not session.config.option.continue_on_collection_errors:
            raise session.Interrupted(f"{session.testsfailed} errors during collection")
        if session.config.option.collectonly:
            return True

        parallel = [it for it in session.items if not self.is_exclusive(it)]
        serial = [it for it in session.items if self.is_exclusive(it)]
        nodes = [self.new_node(f"ln{i}") for i in range(self.n)]
        node_hooks = self._node_hook_callers()
        if node_hooks:
            for n in nodes:
                node_hooks.ready(node=n)
        try:
            if parallel:
                self._run_parallel(session, nodes, parallel)
            if serial and not self.stop.is_set():
                self._run_serial(session, serial)
        finally:
            if node_hooks:
                for n in nodes:
                    node_hooks.down(node=n, error=None)

        if session.shouldfail or session.shouldstop:   # xdist's DSession: Interrupted either way
            raise session.Interrupted(session.shouldfail or session.shouldstop)
        return True

    def _run_parallel(self, session, nodes, items) -> None:
        sched = self.sched
        ids = [it.nodeid for it in items]
        for n in nodes:                      # DSession.worker_workerready
            sched.add_node(n)
        for n in nodes:                      # DSession.worker_collectionfinish
            sched.add_node_collection(n, ids)
        threads = [self.start(n, items) for n in nodes]
        if sched.collection_is_completed:
            sched.schedule()
        self._pump(threads, sched, session, nodes)

    def _run_serial(self, session, items) -> None:
        node = self.new_node("ln-serial")
        node.send_runtest_some(range(len(items)))
        node.shutdown()
        self._pump([self.start(node, items)], None, session, [node])

    def _node_hook_callers(self):
        """xdist's testnodeready/testnodedown, restricted to allowlisted plugins.

        Off unless --lanes-xdist-node-hooks. Restricted because some plugins use
        these hooks as a process protocol (pytest-metadata reads worker output in
        testnodedown and crashed when it got a lane).
        """
        hooks = self.config.hook
        if not (self.config.getoption("lanes_xdist_node_hooks") and hasattr(hooks, "pytest_testnodeready")):
            return None
        pm = self.config.pluginmanager
        allow = self.config.getini("lanes_node_hook_plugins")
        others = [p for name, p in pm.list_name_plugin()
                  if p is not None and not any(tok in str(name) for tok in allow)]
        return SimpleNamespace(
            ready=pm.subset_hook_caller("pytest_testnodeready", remove_plugins=others),
            down=pm.subset_hook_caller("pytest_testnodedown", remove_plugins=others),
        )

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
from .scheduling import add_group_suffix, builtin_dist, make_scheduler


def _timeout_marked(item) -> bool:
    """Whether @pytest.mark.timeout gives this item a non-zero timeout."""
    mark = item.get_closest_marker("timeout")
    if mark is None:
        return False
    value = mark.args[0] if mark.args else mark.kwargs.get("timeout")
    return bool(value)


class SingleProcessSession(LaneRunner):

    def __init__(self, config) -> None:
        super().__init__(config)
        self.sched = None

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        from .probes import PYTEST_TIMEOUT_REFUSED

        if config.pluginmanager.get_plugin("timeout") is not None and any(map(_timeout_marked, items)):
            raise pytest.UsageError(f"{PYTEST_TIMEOUT_REFUSED} (a test has @pytest.mark.timeout)")

        self.sched = make_scheduler(config, self.n)
        if builtin_dist(config) == "loadgroup":   # as xdist's worker: by --dist, not scheduler class
            add_group_suffix(items)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        # Collection errors do not stop the run here, as they do not under xdist:
        # the other tests run and the run fails (exit 1). With -x, the collection
        # error already set session.shouldfail, so the lanes stop at once.
        if session.config.option.collectonly:
            return None      # pytest's own loop: it reports collection errors (Interrupted)

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

        if not self.stopping(session):
            self.ledger.check_complete(session.items)
            self.ledger.raise_if_violated()
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
        threads: list = []
        try:
            for n in nodes:
                threads.append(self.start(n, items))
            if sched.collection_is_completed:
                sched.schedule()
        except KeyboardInterrupt:           # before the pump: lanes may already run tests
            self._interrupt(threads, nodes[:len(threads)])
            raise
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

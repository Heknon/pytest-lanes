"""Hybrid mode, worker side: one xdist worker process running M lanes (touchpoint X2).

xdist starts this process and drives it over an execnet channel. We take over
xdist's worker loop (``WorkerInteractor.pytest_runtestloop``):

* the channel callback receives the controller's ``lanes_runtests`` /
  ``lanes_shutdown`` commands (sent by ``controller.LaneProxy``) and feeds them
  to the lanes' queues;
* after each item, we send xdist's normal ``runtest_protocol_complete`` event;
* reports reach the controller through xdist's own forwarding and serialization,
  which reads ``WorkerInteractor.item_index``, so we set it before each replay.

To the controller and to process-level plugins (pytest-cov, pytest-metadata),
this looks like an ordinary xdist worker.
"""
from __future__ import annotations

import pytest

from .runner import LaneRunner


def find_worker_interactor(pluginmanager):
    """X2. Found by class name: xdist runs remote.py through execnet, so isinstance fails."""
    return next(p for p in pluginmanager.get_plugins() if type(p).__name__ == "WorkerInteractor")


class HybridWorkerSession(LaneRunner):
    set_report_node = False  # reports are serialized to the controller; use report.lane_id

    def lane_workerinput(self, id_: str) -> dict:
        """The process's workerinput (run id, plugin data), naming this lane and
        counting every lane of the run, as controller.LaneProxy does."""
        process = self.config.__dict__["workerinput"]
        return {**process, "workerid": id_, "workercount": process["workercount"] * self.n}

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        interactor = find_worker_interactor(self.config.pluginmanager)
        worker_id = self.config.workerinput["workerid"]
        nodes = [self.new_node(f"{worker_id}.ln{i}") for i in range(self.n)]
        end_of_channel = object()

        def on_command(cmd):  # runs on execnet's receiver thread; only touches thread-safe queues
            if cmd is end_of_channel:
                for n in nodes:
                    n.shutdown()
                return
            name, kwargs = cmd
            if name == "lanes_runtests":
                nodes[kwargs["lane"]].send_runtest_some(kwargs["indices"])
            elif name == "lanes_shutdown":
                nodes[kwargs["lane"]].shutdown()
            elif name == "shutdown":
                for n in nodes:
                    n.shutdown()
            else:
                self.errors.append(RuntimeError(f"pytest-lanes: unsupported xdist command {name!r}"))
                for n in nodes:
                    n.shutdown()

        interactor.channel.setcallback(on_command, endmarker=end_of_channel)
        threads = [self.start(n, session.items) for n in nodes]
        index_of = {it.nodeid: i for i, it in enumerate(session.items)}

        def on_done(node, index, duration):  # the event xdist's worker sends after each item
            interactor.sendevent("runtest_protocol_complete", item_index=index, duration=duration)

        def before_replay(call):
            if call.name == "pytest_runtest_logreport":
                interactor.item_index = index_of[call.kwargs["report"].nodeid]

        self._pump(threads, None, session, nodes, on_done=on_done, before_replay=before_replay)
        return True

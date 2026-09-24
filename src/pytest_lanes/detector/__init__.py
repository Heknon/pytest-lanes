"""The shared-state detector (``--lanes-detect``), an isolated debugging tool.

It answers "which tests change process-wide state?", the state that concurrent
lanes would share: run your suite sequentially with ``--lanes-detect`` and read
the report. It is not used by lanes at run time.

    walk.py       recursive, read-only snapshot of an object graph
    sources.py    what is snapshotted: process state, your modules, plugin objects
    recorder.py   live record of patches applied inside a test body (touchpoint D1)
    classify.py   per-test differences -> findings (per-test, patched, grows, set-once)
    report.py     terminal section and JSON file
    plugin.py     the pytest hooks

Only ``SharedStateDetector`` and ``refuse_concurrent`` are used outside the package.
"""
from .plugin import SharedStateDetector, refuse_concurrent

__all__ = ["SharedStateDetector", "refuse_concurrent"]

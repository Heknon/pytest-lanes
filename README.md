# pytest-lanes (prototype)

Run pytest-xdist's own schedulers on **thread lanes**: in one process, or across many xdist processes. Long, I/O-bound tests can then run thousands-wide without paying 150–500 MB of memory per concurrent test.

```bash
pytest -n 8                 # plain xdist: 8 processes (untouched by this plugin)
pytest --lanes 200          # 1 process x 200 lanes
pytest -n 8 --lanes 25      # 8 xdist processes x 25 lanes each = 200 lanes
```

A lane behaves like an xdist worker. Scheduling decisions come from xdist's real scheduler, fixtures of every scope are cached per lane, and reporters see the same hook stream they would see under `-n`. For the same suite, report-log output is identical in all three modes, and the contract tests enforce that.

**Contents:**
- [Quick start](#quick-start)
- [Using it](#using-it)
- [How it works](#how-it-works)
- [Finding your way around](#finding-your-way-around)
- [Developing](#developing)
- [Documents](#documents)

## Quick start

```bash
uv pip install -e ".[test]"          # or: pip install -e ".[test]"
pytest -p no:warnings --lanes 8      # Python 3.13 or earlier
pytest -X context_aware_warnings=1 --lanes 8   # Python 3.14 (3.14t: on by default)
```

Required flags, which the plugin checks and refuses to run without:

| Condition | Pass |
|---|---|
| Python 3.13 or earlier | `-p no:warnings`, because `warnings.catch_warnings` is not thread-safe there |
| Python 3.14 or later | `-X context_aware_warnings=1` or `PYTHON_CONTEXT_AWARE_WARNINGS=1`. It is the default on free-threaded 3.14t |
| pytest 8.3.5 or earlier | `-p no:threadexception -p no:unraisableexception`, because those versions swap global hooks around every test |

Tested on CPython 3.12, 3.13, 3.14 and 3.14t, with pytest 8.0.2 / 8.3.5 / 9.1.1 and pytest-xdist 3.6.1 / 3.8.0.

## Using it

### Scheduling

Scheduling works exactly as in xdist, and one scheduler definition serves all three modes:

```python
# conftest.py
from xdist.scheduler import LoadScopeScheduling

class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid):     # 'test_x.py::test_step[envB-2]' -> 'envB'
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]

def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)
```

Tests in one scope run sequentially, in order, on one lane; different scopes run in parallel. Without a custom scheduler, choose a built-in one:
- Single-process mode: `--lanes-dist load|loadscope|loadfile|loadgroup` (default `load`).
- Hybrid mode: xdist's own `--dist`.

`each` and `worksteal` are not supported yet.

### Options

| Option / ini / marker | Meaning |
|---|---|
| `--lanes N` | N lanes in this process. In hybrid mode, N lanes *per xdist process*, so the total is `-n` × `--lanes` |
| `--lanes-dist MODE` | Built-in scheduler for single-process mode, when no `pytest_xdist_make_scheduler` returns one |
| `@pytest.mark.lanes_exclusive` | Run this test alone within its process |
| ini `lanes_exclusive_fixtures` | Fixtures that make a test exclusive. Default: capsys, capsysbinary, capfd, capfdbinary, capteesys, recwarn |
| `--lanes-xdist-node-hooks` + ini `lanes_node_hook_plugins` | Single-process mode: fire xdist's `pytest_testnodeready` / `testnodedown` for each lane, but only to the named plugins (default `conftest`) |

Each test report carries `report.lane_id`, such as `ln3` or `gw2.ln3`. In single-process mode `report.node` is the lane, just as it is the worker under xdist.

### What you must know before pointing it at a real suite

Lanes are threads, so anything process-global is shared between concurrently running tests. That includes `mock.patch`, monkeypatching shared modules, `os.environ`, `chdir`, signals, and logging levels. Mark such tests `lanes_exclusive`, or fix them. Other limits:
- A hung thread cannot be killed.
- A crash takes down every lane in its process.
- Output from threads your tests start is attributed to the test only on Python 3.14 with `-X thread_inherit_context=1`.
- `--pdb` is unsupported, as it is under xdist.

The full list, with workarounds, is in [DESIGN.md → Flags](DESIGN.md#flags-no-complete-fix).

## How it works

### The idea

xdist splits a run into a **controller**, which owns the scheduler and the reporters, and **workers**, which run tests. They talk over a small protocol. pytest-lanes keeps that protocol and swaps what sits at each end:

```
pytest --lanes 3                             pytest -n 2 --lanes 3
┌────────────── one process ──────────────┐  ┌──────── controller (xdist DSession) ────────┐
│ main thread = controller                │  │ LaneMux wraps your scheduler:               │
│   xdist scheduler, reporters            │  │   DSession sees 2 workers,                  │
│      │ send_runtest_some  ▲ reports     │  │   scheduler sees 6 LaneProxy nodes          │
│      ▼                    │ (replayed)  │  └───────┬─────────────────────────┬───────────┘
│ ln0 thread  ln1 thread  ln2 thread      │          │ runtests ▼   ▲ reports  │
│ (ThreadNode = one "worker")             │  ┌───────▼────────┐        ┌───────▼────────┐
└─────────────────────────────────────────┘  │ gw0: 3 lanes   │        │ gw1: 3 lanes   │
                                             └────────────────┘        └────────────────┘
```

Two problems have to be solved to run many pytest tests at once in one process:

1. **pytest keeps per-run state that assumes one test at a time.** This covers `SetupState`, fixture caches, capture, log handlers and a couple of races. `isolation.py` and `capture.py` re-key each of these by the current lane, using a contextvar (`LANE`) that is set on each lane thread.
2. **Reporters expect one thread and xdist's hook split.** xdist forwards exactly four hooks from workers to the controller: `pytest_runtest_logstart`, `logreport`, `logfinish` and `warning_recorded`. `hookrouting.py` intercepts those four on lanes, queues them, and the main thread replays them in order. Every other hook runs on the lane, as it would in a worker.

Doing this touches pytest, pluggy and xdist internals. Each one is a numbered **touchpoint** (P1–P7, X1–X4), is checked at startup by `probes.py`, and makes the plugin refuse to run if it has changed. That is the fail-closed rule. The list and the reasons are in [DESIGN.md → Private touchpoints](DESIGN.md#private-touchpoints).

### The life of one test (`--lanes N`)

| Step | Where | What happens |
|---|---|---|
| 1 | `plugin.pytest_configure` | Chooses the mode, runs the startup probes, and registers `SingleProcessSession` |
| 2 | `runner.LaneRunner.pytest_sessionstart` | `isolate_lanes()` installs the per-lane patches; `ControllerHookRouter` starts intercepting the 4 routed hooks |
| 3 | `single.pytest_collection_modifyitems` | `scheduling.make_scheduler` builds the scheduler through xdist's own `pytest_xdist_make_scheduler` hook. Under loadgroup it adds xdist's `@group` nodeid suffix |
| 4 | `single.pytest_runtestloop` | Splits out exclusive tests, creates N `ThreadNode`s, and does what xdist's DSession does: `add_node`, `add_node_collection`, `schedule()` |
| 5 | `lane.ThreadNode.send_runtest_some` | The scheduler hands item indices to a lane, which puts them on the lane's queue |
| 6 | `runner.LaneRunner._node_loop` (lane thread) | xdist's worker loop: it takes an item, looks one ahead to learn `nextitem`, and runs `pytest_runtest_protocol` under the exclusivity lock. Captured output becomes report sections (`capture.capture_phase`) |
| 7 | `hookrouting` → `runner._pump` (main thread) | The routed hooks are queued as `HookCall`s and replayed to reporters. The lane then queues `ItemDone` and waits |
| 8 | `runner._pump` | Calls `scheduler.mark_test_complete`, which may send more work; stops lanes on `-x`/`--maxfail` or when `tests_finished`; then releases the lane |
| 9 | `single._run_serial` | Exclusive tests run alone on one extra lane, `ln-serial` |
| 10 | `runner.LaneRunner.pytest_sessionfinish` | Undoes every patch, in reverse order |

**Hybrid (`-n P --lanes M`)** keeps real xdist processes, so crash detection and worker replacement are xdist's own:
- **Controller** (`controller.py`): `LanesController` wraps whatever scheduler is returned in a `LaneMux`. The scheduler sees P×M `LaneProxy` nodes, while xdist's DSession still sees P workers. `LaneProxy.send_runtest_some` becomes a `lanes_runtests` command to the owning process.
- **Worker** (`worker.py`): `HybridWorkerSession` takes over xdist's worker loop, feeds those commands to M lanes, and after each item sends xdist's normal `runtest_protocol_complete` event. Reports travel through xdist's own serialization, so pytest-cov and pytest-metadata see an ordinary xdist run.

## Finding your way around

```
src/pytest_lanes/
  plugin.py        entry point: options, mode selection (no logic)
  lane.py          ThreadNode (one lane) and the LANE contextvar
  runner.py        LaneRunner: lane threads, main-thread pump, capture hooks, exclusivity lock
  single.py        --lanes N
  worker.py        -n P --lanes M, worker process                       (X2)
  controller.py    -n P --lanes M, controller: LanesController, LaneMux  (X3, X4)
  scheduling.py    building xdist's scheduler; loadgroup suffix          (X1, P5)
  isolation.py     per-lane pytest state                                 (P1, P2, P6, P7)
  capture.py       per-lane stdout/stderr and logging                    (P3)
  hookrouting.py   the 4 controller hooks, replayed on the main thread   (P4)
  probes.py        fail-closed startup checks
tests/test_contract.py   the spec: pytester subprocess tests, incl. parity against plain -n
scripts/matrix.sh        contract suite across Python x pytest/xdist versions (uv)
demo/                    manual smoke run (see demo/README.md)
```

| If you want to… | Look at |
|---|---|
| Understand why a test ran on a given lane | `scheduling.make_scheduler`; the decision itself is xdist's scheduler |
| Change what a lane does per item, or how `-x` stops | `runner.LaneRunner._node_loop`, `_pump` |
| Fix output or log lines attributed to the wrong test | `capture.py` |
| Fix fixture or teardown state leaking between lanes | `isolation.py` (P1 SetupState, P2 fixture caches) |
| Fix a reporter that sees wrong or out-of-order hooks | `hookrouting.py` |
| Change hybrid-mode messaging or crash handling | `controller.py` (controller side), `worker.py` (process side) |
| Add support for a new pytest or xdist version | Run `scripts/matrix.sh`; a failing startup probe names the touchpoint, and the touchpoint's ID leads to its module |
| Touch a new pytest/xdist internal | Don't, unless unavoidable. Otherwise: a context manager in the owning module, a check in `probes.py`, a row in the touchpoint tables (CLAUDE.md, DESIGN.md), and a contract test |

## Developing

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -e ".[test]"
.venv/bin/python -m pytest tests -q -p no:cacheprovider -p no:warnings   # contract suite, ~25s
scripts/matrix.sh                 # 3.12 3.13 3.14 3.14t x 3 pytest/xdist combos (needs PyPI)
RUNS=20 scripts/matrix.sh 3.14t   # repeat runs on one interpreter
```

Rules that keep it correct:
- **The contract tests are the spec.** Every fix starts with a contract test that fails without it.
- **Run on at least two pytest versions** before calling anything done. Free-threaded 3.14t is the best race detector.
- **Contract tests use `runpytest_subprocess`, never in-process pytester.** In-process runs would share the patched `FixtureDef` class and the global hooks.
- **Never weaken the invariants.** They are listed in CLAUDE.md: indistinguishable from an xdist worker, report parity, fail closed, xdist-observing plugins keep working, and no new process-global state in the runner.

## Documents

| File | For |
|---|---|
| README.md | This overview: using the plugin, how it works, where things are |
| [DESIGN.md](DESIGN.md) | Rationale and evidence: touchpoints and why each exists, what was found and fixed, sizing processes × lanes, plugin compatibility, known limitations |
| [CLAUDE.md](CLAUDE.md) | Maintainer and agent brief: invariants, touchpoint table, verified status, prioritized backlog with acceptance criteria |

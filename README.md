# pytest-threadlanes

> **Name.** Distribution `pytest-threadlanes`, import package `pytest_threadlanes`. It was developed as `pytest-lanes`; that name on PyPI belongs to an unrelated project (one subprocess per declared lane) with the same import package and plugin entry, so the two cannot be installed together and `pip install pytest-lanes` gets the other one. The command-line options (`--lanes`, `--lanes-dist`, …), markers and ini settings keep the `lanes` names. The repository is `Heknon/pytest-threadlanes`.

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
- [Releasing](#releasing)
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

Supported: pytest 8.x–9.x and pytest-xdist 3.6–3.x (`pyproject.toml`). Tested on CPython 3.12, 3.13, 3.14 and 3.14t, with pytest 8.0.2 / 8.3.5 / 9.1.1 and pytest-xdist 3.6.1 / 3.8.0; other versions in range are untested, and a changed internal makes the plugin refuse to start rather than run incorrectly.

## Using it

### Scheduling

Scheduling works exactly as in xdist, and one scheduler definition serves all three modes. The recommended one for environments:

```python
# conftest.py
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
```

Tests in one scope run sequentially, in order, on one lane; different scopes run in parallel.

Why `_reschedule`: xdist's loadscope gives a lane (or worker) its next environment as soon as its current one is down to two tests, so that environment waits behind both while other lanes may sit idle. With hour-long tests that is hours. The override waits until one test is left. It cannot wait for zero: a lane, like an xdist worker, starts its last queued test only once it knows what comes next (to decide which fixtures to tear down), so at zero it would never finish. The override behaves the same under plain `-n`. It uses loadscope's private `_reschedule`, `_pending_of` and `assigned_work`; `tests/test_contract.py` runs it verbatim in all three modes, so a change in xdist shows up in `scripts/matrix.sh`.

Without a custom scheduler, choose a built-in one:
- Single-process mode: `--dist` as with xdist, or `--lanes-dist load|loadscope|loadfile|loadgroup` (default `load`).
- Hybrid mode: xdist's own `--dist`.

`each` and `worksteal` are not supported yet, and are refused at startup. Note that xdist's `worksteal` moves single tests between workers, so it would split an environment's steps across lanes; for environments only a steal of whole, not-yet-started scopes would be safe (backlog 7).

### Options

| Option / ini / marker | Meaning |
|---|---|
| `--lanes N` | N lanes in this process. In hybrid mode, N lanes *per xdist process*, so the total is `-n` × `--lanes`. `--lanes 0` turns lanes off |
| `--lanes-dist MODE` | Built-in scheduler for single-process mode, when no `pytest_xdist_make_scheduler` returns one. Defaults to xdist's `--dist` if given, else `load` |
| `@pytest.mark.lanes_exclusive` | Run this test alone within its process. Doctests always are, since doctest swaps `sys.stdout` for the whole process |
| ini `lanes_exclusive_fixtures` | Fixtures that make a test exclusive. Default: capsys, capsysbinary, capfd, capfdbinary, capteesys, recwarn, and pytest-cov's no_cover (it pauses coverage for the whole process; the `no_cover` marker makes a test exclusive too). Requesting one at run time (`request.getfixturevalue`) from a test that is not exclusive fails that test with instructions |
| `-s` / `--capture=no` | As under xdist: test output goes straight to the terminal. Log records are still captured per test |
| `--lanes-allow-patches`, ini `lanes_allow_patches`, `@pytest.mark.lanes_allow_patches` | Turn off the patch guard for the run, or for one test. The guard fails a test that is not `lanes_exclusive` when it patches process-wide state through `mock.patch`/pytest-mock/`monkeypatch` (a module or class attribute, a dotted path, the environment, `chdir`, `sys.path`); patches of instances are allowed. Direct environment writes (`os.environ[k] = v`) and `os.chdir` are guarded too: besides being seen by every lane, an environment write while another lane starts a subprocess makes that spawn fail. Set values the whole run needs in `pytest_configure`, not in a session-scoped fixture: each lane tears its own fixture down when it finishes |
| ini `lanes_interrupt_grace` | Seconds to wait after Ctrl-C for the interrupted lanes to run their teardown (default 30). A second Ctrl-C stops waiting |
| `--lanes-xdist-node-hooks` + ini `lanes_node_hook_plugins` | Single-process mode: fire xdist's `pytest_testnodeready` / `testnodedown` for each lane, but only to the named plugins (default `conftest`) |

Each lane is its own xdist worker: `worker_id`, `testrun_uid`, `xdist.get_xdist_worker_id(request)` and `config.workerinput["workerid"]` give the lane, such as `ln3` or `gw2.ln3`, so resources named after the worker do not collide. `os.environ["PYTEST_XDIST_WORKER"]` (and `_COUNT`) read on a lane name the lane too; a subprocess sees them only if you pass `env=os.environ.copy()`. Each test report carries `report.lane_id`. In single-process mode `report.node` is the lane, just as it is the worker under xdist.

### What you must know before pointing it at a real suite

Lanes are threads, so anything process-global is shared between concurrently running tests. That includes `mock.patch`, monkeypatching shared modules, `os.environ`, `chdir`, signals, logging levels, `random.seed`, `socket.setdefaulttimeout` and `locale.setlocale`. Mark such tests `lanes_exclusive`, or fix them; `pytest --lanes-detect` (below) finds them. Handled for you:
- A `mock.patch`, pytest-mock or `monkeypatch` patch of shared state in a test that is not `lanes_exclusive` fails the test and says what to do (the patch guard; see `--lanes-allow-patches`).
- `contextlib.redirect_stdout`/`redirect_stderr` redirect only the lane that entered them.
- Replacing `sys.stdout` directly, as click's `CliRunner` does, cannot be made per lane: the run fails and names the tests. Mark them `lanes_exclusive`.
- A test reading stdin fails at once, as under pytest's capture.
- `PYTEST_CURRENT_TEST` is per lane: `os.environ["PYTEST_CURRENT_TEST"]` names the test running on that lane. It is not written to the process environment (that broke other lanes' subprocess spawns), so a subprocess sees it only if you pass `env=os.environ.copy()`.
- Ctrl-C interrupts the running tests and runs their teardown (see `lanes_interrupt_grace`).

Other limits:
- A hung thread cannot be killed. pytest-timeout is refused in single-process mode (on a timeout it would end the whole process) but works in hybrid mode, where xdist replaces the worker. `faulthandler_timeout` is refused in both modes.
- A crash takes down every lane in its process: every test in flight there is reported as crashed, as xdist reports the one test of a crashed worker. Under a loadscope-based scheduler each of them is then run again, from the step that was running, as xdist does for its crashed test. Choose `-n` for the blast radius you accept (see DESIGN.md → Sizing).
- Output from threads your tests start is attributed to the test only on Python 3.14 with `-X thread_inherit_context=1`.
- Before Python 3.14, `pytest.warns`, `pytest.deprecated_call` and `recwarn` change process-wide warning state, so a test using them must be `lanes_exclusive`. Otherwise it fails and says so. `warnings.catch_warnings` used directly is not guarded.
- `--pdb` is unsupported, as it is under xdist, and so is `--trace` in single-process mode; a `breakpoint()` on a lane cannot read the terminal either. To debug a test, run it without `--lanes` (and without `-n`): the same scheduler, fixtures and code, in plain pytest.

The full list, with workarounds, is in [DESIGN.md → Flags](https://github.com/Heknon/pytest-threadlanes/blob/HEAD/DESIGN.md#flags-no-complete-fix).

### Finding shared state: `--lanes-detect`

Before running a suite on lanes, find the tests that change process-wide state:

```bash
pytest --lanes-detect --lanes-detect-report=shared-state.json   # sequential; no --lanes, no -n
```

Tests run one at a time, as in plain pytest. Around each test the detector snapshots process state (environment variables, cwd, `sys.path`, logging levels, signal handlers) and, recursively, everything reachable from your modules' globals, their classes' attributes, and every registered plugin object (including plugins held inside other plugins). It also records every `mock.patch`/`patch.dict`/pytest-mock patch, `monkeypatch` call, `os.environ` write and `os.chdir` made inside a test body. A session, module or class fixture's changes are reported under the fixture's name, not the test that ran it. A value set and restored inside one test body (for example by a context manager) is not seen, unless it goes through mock or monkeypatch. The report sorts each changed path:

| Kind | Meaning | Action |
|---|---|---|
| `UNSAFE per-test` | Changes while tests run: a "current test" field, a global set by a fixture | Make it per lane (a contextvar), or mark the tests `lanes_exclusive` |
| `UNSAFE patched` | Patched inside a test | Mark the test `lanes_exclusive` |
| `CHECK grows` | A container that grows with every test | Check it is thread-safe |
| `OK set-once` | Set once, then stable: a cache, lazy initialisation | Nothing, if it is safe to share between threads |

| ini | Meaning |
|---|---|
| `lanes_detect_ignore` | Paths shared on purpose (fnmatch patterns, one per line), e.g. `module:myinfra.clients._CACHE` |
| `lanes_detect_modules` | Installed packages to inspect as well (by default: modules under the rootdir) |
| `lanes_detect_max_depth`, `lanes_detect_max_nodes` | Walk limits (12 levels, 200,000 values per snapshot) |

The walk is read-only: it never evaluates properties or `__getattr__`, and keeps no string values (only their length and hash).

## How it works

### The idea

xdist splits a run into a **controller**, which owns the scheduler and the reporters, and **workers**, which run tests. They talk over a small protocol. pytest-threadlanes keeps that protocol and swaps what sits at each end:

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

Doing this touches pytest, pluggy and xdist internals. Each one is a numbered **touchpoint** (P1–P15, C1–C2, X1–X4), is checked at startup by `probes.py`, and makes the plugin refuse to run if it has changed. That is the fail-closed rule. The list and the reasons are in [DESIGN.md → Private touchpoints](https://github.com/Heknon/pytest-threadlanes/blob/HEAD/DESIGN.md#private-touchpoints).

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
src/pytest_threadlanes/
  plugin.py        entry point: options, mode selection (no logic)
  lane.py          ThreadNode (one lane) and the LANE contextvar
  runner.py        LaneRunner: lane threads, main-thread pump, capture hooks, exclusivity  (P9)
  single.py        --lanes N
  worker.py        -n P --lanes M, worker process                       (X2)
  controller.py    -n P --lanes M, controller: LanesController, LaneMux  (X3, X4)
  scheduling.py    building xdist's scheduler; loadgroup suffix          (X1, P5)
  isolation.py     per-lane pytest state, worker identity and environment,
                   warns guard, patch guard                              (P1, P2, P6, P7, P10, P11, P12, P14)
  capture.py       per-lane stdout/stderr, redirects and logging          (P3, P8, P13, P15)
  hookrouting.py   the 4 controller hooks, replayed on the main thread   (P4)
  compat.py        shims for third-party plugins (pytest-rerunfailures)  (C1, C2)
  integrity.py     run-time check: reports match what lanes ran, else INTERNALERROR
  detector/        --lanes-detect, a separate debugging tool: walk, sources, recorder (D1),
                   classify, report, plugin
  probes.py        fail-closed startup checks
tests/                   the spec: pytester subprocess tests (contract, parity against plain -n,
                         robustness, isolation, integrity, patch guard, detector)
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
| Understand an "integrity check failed" INTERNALERROR | `integrity.py` |
| Change hybrid-mode messaging or crash handling | `controller.py` (controller side), `worker.py` (process side) |
| Add support for a new pytest or xdist version | Run `scripts/matrix.sh`; a failing startup probe names the touchpoint, and the touchpoint's ID leads to its module |
| Touch a new pytest/xdist internal | Don't, unless unavoidable. Otherwise: a context manager in the owning module, a check in `probes.py`, a row in the touchpoint tables (CLAUDE.md, DESIGN.md), and a contract test |

## Developing

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -e ".[test]"
.venv/bin/python -m pytest tests -q -p no:cacheprovider -p no:warnings -n 4   # ~300 tests, ~2 min
scripts/matrix.sh                 # 3.12 3.13 3.14 3.14t x 3 pytest/xdist combos (needs PyPI)
RUNS=20 scripts/matrix.sh 3.14t   # repeat runs on one interpreter
```

Rules that keep it correct:
- **The contract tests are the spec.** Every fix starts with a contract test that fails without it.
- **Run on at least two pytest versions** before calling anything done. Free-threaded 3.14t is the best race detector.
- **Contract tests use `runpytest_subprocess`, never in-process pytester.** In-process runs would share the patched `FixtureDef` class and the global hooks.
- **Never weaken the invariants.** They are listed in CLAUDE.md: indistinguishable from an xdist worker, report parity, fail closed, xdist-observing plugins keep working, no new process-global state in the runner, and silent corruption made loud.

## Releasing

The version lives only in `pyproject.toml`. A release is a tag `vX.Y.Z` that matches it; pushing the tag runs `.github/workflows/release.yml`:

1. **build**: checks the tag against `pyproject.toml`, builds the wheel and sdist, `twine check --strict`, and loads the plugin from the installed wheel;
2. **test**: the suite once (Python 3.12, pytest 9.1.1, xdist 3.8.0), against the built wheel;
3. **publish**: to PyPI with Trusted Publishing (no token is stored in GitHub);
4. **github-release**: a GitHub Release with the wheel, the sdist, and the version's `CHANGELOG.md` section as notes.

To release:

```bash
# 1. bump version in pyproject.toml and add a "## X.Y.Z" section to CHANGELOG.md
scripts/matrix.sh                         # the real gate: 3.12-3.14t x pytest/xdist combos
git commit -am "Release X.Y.Z" && git push
git tag vX.Y.Z && git push origin vX.Y.Z  # starts the release workflow
```

A manual run of the workflow (Actions → release → Run workflow) builds and tests without publishing: a dry run.

One-time setup, before the first release:
- On pypi.org → Your account → Publishing → *Add a new pending publisher*: project `pytest-threadlanes`, owner `Heknon`, repository `pytest-threadlanes`, workflow `release.yml`, environment `pypi`.
- In the GitHub repository → Settings → Environments, create `pypi`. Adding yourself as a required reviewer there makes every publish wait for your approval.

## Documents

| File | For |
|---|---|
| README.md | This overview: using the plugin, how it works, where things are |
| [DESIGN.md](https://github.com/Heknon/pytest-threadlanes/blob/HEAD/DESIGN.md) | Rationale and evidence: touchpoints and why each exists, what was found and fixed, sizing processes × lanes, plugin compatibility, known limitations |
| [CHANGELOG.md](https://github.com/Heknon/pytest-threadlanes/blob/HEAD/CHANGELOG.md) | What changed in each release |
| [CLAUDE.md](https://github.com/Heknon/pytest-threadlanes/blob/HEAD/CLAUDE.md) | Maintainer and agent brief: invariants, touchpoint table, verified status, prioritized backlog with acceptance criteria |

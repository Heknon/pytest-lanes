# CLAUDE.md: pytest-threadlanes

Read this file first, then DESIGN.md (design, touchpoints and why, correctness defences, limitations). README.md is the user guide.

## What this is and why

*Package:* `pytest-threadlanes` (import `pytest_threadlanes`, plugin entry `threadlanes`); options, markers and ini settings use the `lanes` prefix.

We have a pytest suite of long, I/O-bound tests. Each test takes 1–2 hours, and we need to run thousands of them concurrently. pytest-xdist gives one process per concurrent test, at 150–500 MB each, so memory caps our concurrency. The suite's infrastructure is hundreds of thousands of lines of synchronous code, so converting it to async is not an option.

pytest-threadlanes runs pytest-xdist's own scheduler objects on thread "lanes":

- `pytest -n 8`: plain xdist, which is the baseline and is unchanged.
- `pytest --lanes 200`: one process with 200 thread lanes.
- `pytest -n 8 --lanes 25`: 8 real xdist processes with 25 lanes each, 200 lanes in total.

Tests that share an environment must run sequentially, in order, on one lane. Different environments run in parallel. The user supplies scheduling as a normal xdist custom scheduler, and that exact code must work in all three modes:

```python
class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid): ...
def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)
```

## Non-negotiable invariants

Any change that breaks one of these is a regression.

1. **Indistinguishable from xdist.** A lane must behave like an xdist worker in three ways:
   - **Scheduling:** decisions come from xdist's real scheduler object.
   - **Fixtures:** every scope is cached per lane, as it would be per worker.
   - **Hook split:** only `pytest_runtest_logstart`, `logreport`, `logfinish` and `warning_recorded` are replayed on the controller/main thread. That is exactly the set xdist forwards. One implementation of them runs on the lane instead: pytest's own `Session.pytest_runtest_logreport` (failure counting for `-x`/`--maxfail`), because in xdist it runs synchronously inside the worker and the runner depends on it before tearing a failing test down.
2. **Report parity.** For the same suite, report-log output (nodeid, when, outcome, and section names in order) must be identical under `-n N --dist X`, `--lanes N --lanes-dist X`, and `-n N --lanes M --dist X`. The parity tests in `tests/` enforce this.
3. **Fail closed.** Every use of a pytest, pluggy or xdist internal is feature-probed at startup. If a probe fails, the plugin raises `UsageError` with an explanation instead of running incorrectly. Never add a new internal touchpoint without all three of: a probe, an entry in the touchpoint table below and in DESIGN.md, and a contract test.
4. **Plugins that observe xdist must keep working.** Terminal, junitxml, report-log, pytest-html, pytest-metadata, pytest-cov, rerunfailures, and the user's own failure-instrumentation plugin. xdist node hooks go only to allowlisted plugins in single-process mode, because pytest-metadata uses `pytest_testnodedown` as a data-transfer protocol and crashed when it received them.
5. **No new dependency on process-global state in the runner.** Per-lane state lives on the `ThreadNode`, keyed by the `_LANE` contextvar.
6. **Silent corruption must be loud.** A run whose report stream doesn't match what its lanes ran fails with INTERNALERROR (`integrity.py`), in every mode; never downgrade that to a warning. In hybrid mode a worker's INTERNALERROR fails the run, although plain xdist 3.8.0 exits 0 on one.

## File map

The package is split by responsibility. Read `plugin.py`'s docstring first: it has the same map.

| Module | What it holds | Touchpoints |
|---|---|---|
| `plugin.py` | Entry point: options, and `pytest_configure` choosing the mode. No logic (pytest registers it as a plugin, so any `pytest_*` name here is a hook) | |
| `lane.py` | `ThreadNode`, one lane: an xdist WorkerController look-alike that also owns its private SetupState, fixture caches, output buffers and log handlers. The `LANE` contextvar | |
| `runner.py` | `LaneRunner`, the base of both test-running sessions: session-long install/uninstall, per-phase capture, the lane loop (`_node_loop`, a copy of xdist's `WorkerInteractor` loop), the main-thread pump (`_pump`), Ctrl-C handling (`_interrupt`: `KeyboardInterrupt` raised in each lane via the C API `PyThreadState_SetAsyncExc`, teardown on the lane, `lanes_interrupt_grace`), and `ReadWriteLock` for exclusive tests | P9 |
| `single.py` | `SingleProcessSession` (`--lanes N`): plays xdist's DSession on the main thread, then runs exclusive tests on `ln-serial` | |
| `worker.py` | `HybridWorkerSession` (a worker of `-n P --lanes M`): takes over xdist's worker loop and channel | X2 |
| `controller.py` | `LanesController`, `LaneMux`, `LaneProxy` (controller of `-n P --lanes M`): presents P real workers to xdist's DSession and P×M virtual lanes to the scheduler. Passes the controller's `-X context_aware_warnings`/`thread_inherit_context` to the workers through the environment (xdist starts workers without `-X` options) | X3, X4 |
| `scheduling.py` | `make_scheduler` (via xdist's own factory hook), the scheduler protocol check, the loadgroup `@group` suffix | X1, P5 |
| `isolation.py` | Per-lane pytest state, one context manager per touchpoint, and `isolate_lanes()` that installs them all. The patch guard (`guard_process_patches`) | P1, P2, P6, P7, P10, P11, P12, P14 |
| `capture.py` | Per-lane stdout/stderr and logging; per-lane `contextlib.redirect_stdout`/`redirect_stderr`; `sys.stdin` that fails a read as pytest's capture does; suspending global capture on a lane also suspends the test's capture fixture; logging's logger registry made safe to iterate | P3, P8, P13, P15 |
| `hookrouting.py` | `ControllerHookRouter`: the 4 controller hooks queued on lanes and replayed on the main thread | P4 |
| `compat.py` | Shims for third-party plugins that assume one test at a time per process | C1, C2 |
| `integrity.py` | `Ledger`: the run-time integrity check. Each lane's routed hooks name the item it runs, replayed streams nest (logstart, reports, logfinish), each done item was logged, and (single process) every collected item ran as often as collected. `StdioWatch`: `sys.stdout`/`stderr`/`stdin` replaced while two or more lanes run tests (click's `CliRunner`) fails the run | |
| `probes.py` | Fail-closed startup checks, one function per touchpoint | all except P1 |
| `detector/` | `--lanes-detect`, an isolated debugging tool, not used by lanes at run time: which tests change process-wide state. `walk.py` (read-only recursive snapshot), `sources.py` (process state, modules under the rootdir, plugin objects), `recorder.py` (live patch recording), `classify.py` (per-test / patched / grows / set-once), `report.py`, `plugin.py`. Only `SharedStateDetector` and `refuse_concurrent` are imported outside it | D1 |

Mode selection in `pytest_configure`:
- `config.workerinput` present: hybrid worker, `HybridWorkerSession`.
- `-n` or `--tx`: hybrid controller, `LanesController`.
- Otherwise: `SingleProcessSession`.

Other files:

- `tests/` is the spec: pytester subprocess tests (about 300) in `test_contract.py` (the original contract), `test_parity.py` (report parity in all modes), `test_robustness.py` (failure paths, options, run shapes), `test_isolation.py` (output, logging, basetemp, worker identity and environment, warnings), `test_integrity.py` (the integrity check, and a canary suite under maximum thread-switching pressure), `test_patch_guard.py` (P14) and `test_detector.py` (`--lanes-detect`). Shared helpers live in `tests/lanes_testing.py`.
- `demo/` is a manual smoke test (see `demo/README.md`).
- `scripts/matrix.sh` runs the suite against several pytest/xdist versions, in separate venvs.
- `.github/workflows/ci.yml` is the CI workflow, run by hand only (`workflow_dispatch`): GitHub runners are paid.
- `.github/workflows/release.yml` publishes to PyPI (Trusted Publishing) and makes a GitHub Release when a `vX.Y.Z` tag matching `pyproject.toml`'s version is pushed; it tests the built wheel on one combo first. A manual run is a dry run. `CHANGELOG.md` gets a section per release (README → Releasing). Never push a release tag without the user's go-ahead: it publishes.

## Internal touchpoints

All are probed at startup (`probes.py`) except P1 (`session._setupstate`), which the contract tests cover.

| # | Touchpoint | Purpose |
|---|---|---|
| P1 | `session._setupstate` | Router giving each lane its own `SetupState` |
| P2 | `FixtureDef.cached_result` / `._finalizers` / `.cached_param` | Class-level properties keyed per lane. `cached_param` is set and deleted by pytest's setuponly plugin (`--setup-show`/`--setup-only`); shared, one lane deleted it while another printed it (AttributeError, 3 of 5 runs on 3.14t). Probed: `FixtureDef` has no `__slots__` and supports weak references, and pytest's setuponly plugin still uses `cached_param` |
| P3 | `LoggingPlugin.caplog_handler` / `.report_handler`; `catching_logs.__enter__` (does it attach to non-propagating loggers: pytest ≥ 9) | Per-lane dispatch; a permanent root `_LogRouter` makes pytest's concurrent handler attach/detach harmless. Non-propagating loggers are routed as the installed pytest captures them: those existing at each phase start (pytest 9), none (pytest 8). Probed: the plugin's `caplog_handler`/`report_handler`/`log_level`, and its handler class (built without arguments, with `records`/`stream`/`reset`/`clear`). `caplog.handler.setFormatter`/`addFilter` reach the lane's handler; records of threads that are not lanes are dropped (they belong to no test) |
| P4 | pluggy `PluginManager._inner_hookexec` | Routes the 4 controller hooks to the main thread (the same slot pluggy's public `add_hookcall_monitoring` uses) |
| P5 | `item._nodeid`; xdist's `WorkerInteractor.pytest_collection_modifyitems` (called unbound: it does not use `self`) | `@group` suffix under loadgroup, computed by the installed xdist's own code (3.6: the closest mark; 3.8: every mark). Probed: its signature |
| P6 | `_pytest.runner._update_current_test_var`; `os.environ.__class__` (a subclass of `os._Environ`) | `PYTEST_CURRENT_TEST`, `PYTEST_XDIST_WORKER` and `PYTEST_XDIST_WORKER_COUNT` per lane, never written to the process environment; an inherited `PYTEST_CURRENT_TEST` is removed for the session. pytest sets and deletes it (`putenv`/`unsetenv`) at every phase: with many lanes the C environment was rewritten constantly, and a subprocess started meanwhile without `env=` failed with `OSError: [Errno 14] Bad address`, or could get a torn environment. It also named another lane's test (F17) and raised KeyError when lanes finished together. Now `os.environ[...]`/`getenv`/`copy()` on a lane return that lane's value; children started without `env=` do not inherit it. Probed: the class can be replaced, and pytest's value format is still `"{nodeid} ({when})"` `PYTEST_XDIST_TESTRUNUID` is per lane too, as xdist sets it in each worker |
| P7 | `config._tmp_path_factory` (`getbasetemp`, `_given_basetemp`, `_basetemp`), `config._tmpdirhandler._tmppath_factory` | A basetemp per lane, as xdist gives each worker: `<root>/ln3` (single process) or `<root>/popen-gw0.ln3` (hybrid), so `getbasetemp().parent` stays the run root shared by all workers. The process's own basetemp is created under a lock: pytest creates it lazily without one, and with `--basetemp` two lanes both `rmtree`+`mkdir`ed it. Needs lanes' `pytest_configure` to be `trylast` so the probe runs after the tmpdir plugin configures. At session end pytest's `_pytest.pathlib.cleanup_dead_symlinks` is run on each lane's basetemp when it exists (optional: without it, dead links stay, as in older pytest) |
| P8 | `logging.Logger.manager.loggerDict` | pytest ≥ 9 iterates it whenever a test phase starts; a logger created on another lane at that moment raised "dictionary changed size" (reproduced 5/5 on 3.12 with 64 lanes). Replaced for the session by a dict subclass whose views come from an atomic copy |
| P9 | `_pytest.doctest.DoctestItem` | Doctest items run exclusively: doctest's runner swaps `sys.stdout` for the whole process, which captured other lanes' output and failed the doctest |
| P10 | `config.__class__` (a subclass with per-lane `workerinput`/`workeroutput` properties) | Each lane is its own xdist worker: `worker_id`, `testrun_uid`, `xdist.get_xdist_worker_id()` and `config.workerinput` name the lane (`ln3`, or `gw0.ln3` in hybrid, counting every lane of the run). The single-process main thread keeps the controller role (no `workerinput`). Probed: Config has no `__slots__`, and xdist still reads `config.workerinput` |
| P11 | `_pytest.recwarn.WarningsRecorder.__enter__` | Without context-aware warnings (Python < 3.14), `pytest.warns`/`deprecated_call`/`recwarn` swap process-wide warning state (5 of 6 concurrent blocks failed). A non-exclusive test entering one fails with instructions to mark it `lanes_exclusive`. Not installed when warnings are context-aware |
| P12 | `_pytest.cacheprovider.Cache.get` / `.set` | Serialized by one process-wide lock: pytest writes a value by truncating the file and then writing it, so a lane reading concurrently got the default (45 of 200 get-after-set tests failed) |
| P13 | stdlib `contextlib._RedirectStream.__enter__` / `__exit__` (`._stream`, `._new_target`) | `redirect_stdout`/`redirect_stderr` entered on a lane push the target on that lane's own stack instead of replacing `sys.stdout` for the process, which captured every other lane's prints (4 of 4 redirecting tests failed). Only while lanes capture (not with `-s`) |
| P14 | stdlib `unittest.mock._patch.__enter__` (`.getter`: a `partial(pkgutil.resolve_name, path)` for dotted paths, `.attribute`), `_patch_dict._patch_dict` (`.in_dict`); public `pytest.MonkeyPatch` methods | The patch guard: in a test that is not exclusive, a patch of a module or class attribute, anything by dotted path, `os.environ`/`sys.modules`, `setenv`/`delenv`, `chdir` or `syspath_prepend` fails the test with instructions. Session-scoped fixtures are guarded too (each lane tears its own down when it finishes, undoing the patch for lanes still running: seen as a KeyError); the message says to set such values in `pytest_configure`. Exempt: instances and classes defined in a function, pytest's own patches (its twisted support), `lanes_exclusive` and `lanes_allow_patches` tests, processes with one lane; `--lanes-allow-patches` / ini `lanes_allow_patches` turn it off. Direct writes (`os.environ[k] = v`, `del`, `os.putenv`, `os.chdir`) are guarded too, through the public audit events `os.putenv`/`os.unsetenv`/`os.chdir` (no internal): an environment write while another lane spawns a subprocess breaks that spawn. Not guarded: objects no module holds (an `lru_cache`/`get_settings()` singleton, an entry reached only through another object: presumed the test's own; `--lanes-detect` reports them), forked children (their state is their own), a `chdir` to the current directory, writes while a module is being imported (once per process), and pytest-cov/coverage (invariant 4) |
| P15 | `CaptureManager.suspend_global_capture` / `resume_global_capture` (`_capture_fixture`, `suspend_fixture`, `resume_fixture`) | On a lane, they also suspend/resume the test's capture fixture. Plugins write to the terminal between the two (`--setup-show`); in plain pytest that bypasses capfd as a side effect of suspending global capture, which lanes turn off, so the line went into the test's capfd. Probed, with `CaptureFixture._is_started` |
| C1 | pytest-rerunfailures ≥ 15: `config.failures_db` (`ClientStatusDB`) | A hybrid worker's lanes shared its one socket to the controller; interleaved request/response pairs killed a lane with `ValueError`. Every method of the client is serialized (16.7 added socket methods a fixed list missed) |
| C2 | pytest-rerunfailures ≥ 16: module-level `suspended_finalizers` | Parks the setup stack of a test about to be rerun; every test's teardown restores it into its own SetupState. Shared, another lane took it (fixtures torn down mid-module, or never). Replaced by a per-lane dict for the session; probed (a dict read by name in `_restore_suspended_finalizers`) |
| X1 | xdist scheduler protocol | Uses `add_node`, `add_node_collection`, `schedule`, `mark_test_complete`, `remove_node`, `tests_finished`, `collection_is_completed`, `has_pending`, `nodes`, `numnodes`. Checked on each scheduler instance, in single-process and hybrid mode, never by import name: xdist 3.6.1 lacks `parse_tx_spec_config` |
| X2 | hybrid worker: `WorkerInteractor.channel`, `.sendevent`, `.item_index` | Located by class name, because xdist executes `remote.py` via execnet and `isinstance` fails. Probed in the worker (its report forwarder must still read `item_index`) |
| X3 | hybrid controller: `DSession.handle_crashitem` | Used for the 2nd and later crashed lanes of one worker |
| X4 | hybrid controller: `WorkerController.workerinput` / `workerinfo` / `workeroutput` | Mirrored on each `LaneProxy`, so a custom scheduler or plugin reading them sees a worker (`workerinput` gets the lane's id and the total lane count). Probed by checking the xdist code that sets them `LaneProxy.shutting_down` also follows the worker's `WorkerController.shutting_down` (down, or told to shut down), so the scheduler never sends to a dead or draining worker; probed as a property |
| D1 | `--lanes-detect` only: stdlib `unittest.mock._patch.__enter__` (and `.getter`/`.attribute`), `_patch_dict._patch_dict` / `._unpatch_dict`; `contextlib._RedirectStream` (to tell redirects from stdio swaps) | Records patches applied inside a test body, which no snapshot sees. Probed by `detector.recorder.check_d1` (a UsageError for `--lanes-detect` if it fails); never installed in a lanes run |

## How to run

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -e ".[test]"
.venv/bin/python -m pytest tests -q -p no:cacheprovider -p no:warnings -n 4   # ~300 tests, ~2 min
scripts/matrix.sh                        # 3.12 3.13 3.14 3.14t x 3 pytest/xdist combos (needs PyPI)
RUNS=20 scripts/matrix.sh 3.14t          # repeat runs, one interpreter
```

`scripts/matrix.sh` uses uv. Use a uv new enough to know CPython 3.14 final (uv 0.8.x only has 3.14.0rc2); `uvx --from uv uv python install 3.14 3.14t` works if the installed uv is older.

Required flags and environment:

- On Python 3.13 or earlier, `-p no:warnings` is mandatory, because `catch_warnings` is not thread-safe there and the plugin refuses to run otherwise. On Python 3.14 or later, use `-X context_aware_warnings=1` (or `PYTHON_CONTEXT_AWARE_WARNINGS=1`) instead; free-threaded 3.14t has it on by default.
- pytest 8.3.5 and earlier also need `-p no:threadexception -p no:unraisableexception`, because those versions swap global hooks per test. `tests/` adds these flags automatically when needed.
- Tests use `runpytest_subprocess`. Keep it that way: in-process pytester would share the patched `FixtureDef` class and the global hooks.

## Status

- The suite (`tests/`, about 300 tests) passes on the full matrix: CPython 3.12, 3.13, 3.14 and 3.14t, each with pytest 8.0.2 / xdist 3.6.1, 8.3.5 / 3.6.1 and 9.1.1 / 3.8.0.
- Stress runs are described in DESIGN.md → Verification. Report-log output is identical to plain xdist in all modes; a crash in hybrid mode is recovered by xdist.
- Scale (1-core sandbox): 1,000 concurrent 3-second tests took 4.4s and 93 MB as 1 × 1000; 7.4s and 244 MB as 4 × 250; 14.5s and 848 MB as 20 × 50.
- Released through `.github/workflows/release.yml` (README → Releasing); changes go in `CHANGELOG.md`.

## Known issues (DESIGN.md → Limitations)

These are not bugs to "fix" by weakening the invariants.

- **Process-global state** in user tests and infrastructure: `mock.patch`, monkeypatch on shared modules, `os.environ`, `chdir`, signals. Patches through `mock`/pytest-mock/`monkeypatch`, and direct environment writes and `os.chdir`, fail closed (P14, opt-out `--lanes-allow-patches`); other direct assignments (`module.attr = x`) cannot be seen at run time: `--lanes-detect` finds those still set when a test's call phase ends (not ones set and restored inside the test body). Set run-wide environment variables in `pytest_configure`.
- **Warnings** before Python 3.14.
- **Child-thread output attribution** before Python 3.14.
- **Unkillable hung threads.**
- **Crash collateral:** every in-flight test in a crashed process is reported as crashed (then rerun under a loadscope-based scheduler, as xdist reruns the culprit).
- **Exclusive tests pause their whole process.**
- **Unsupported:** `each` and `worksteal` modes, and `--pdb` (debug a test by running it without `--lanes`/`-n`).
- **Ctrl-C** interrupts every lane and runs its teardown, as pytest does; a lane blocked in one long C call (a sleep, a socket read without timeout) is abandoned after `lanes_interrupt_grace` (30s) and named. SIGTERM kills the process without teardown, in every mode.
- **Process-wide setters and stdio swaps:** `random.seed`, `socket.setdefaulttimeout`, `locale.setlocale`, `os.umask` etc. are shared by all lanes (`--lanes-detect` reports them); replacing `sys.stdout` (click's `CliRunner`) fails the run unless the test is `lanes_exclusive`. `contextlib.redirect_stdout` is per lane.
- **Thread pools shared between tests** keep the lane context of the test that started their threads (DESIGN.md F18).
- **Also open (DESIGN.md F12–F16):**
  - `warnings.catch_warnings` used directly (not via pytest.warns) on Python ≤ 3.13;
  - per-test timeouts: pytest-timeout (single-process) and `faulthandler_timeout` are refused until `--lanes-timeout` (backlog 3);
  - `signal.signal` fails in lanes;
  - worker-side `logreport` consumers see reports late;
  - per-item overhead on GIL builds.

## Backlog, in priority order, with acceptance criteria

1. **Validate on the user's real suite.** Run `--lanes-detect` on it (with the infrastructure package in `lanes_detect_modules`), fix or mark what it reports, then run a slice under `-n`, `--lanes` and `-n --lanes` and diff report-log.
2. **Runner-level per-test timeout (F4, F13).**
   - Add a `--lanes-timeout` option and a `lanes_timeout` marker.
   - On expiry: report the test failed with a stack dump of that lane's thread; try `PyThreadState_SetAsyncExc` as a best effort; if the thread doesn't return within a grace period, mark the lane dead and abandon it.
   - Single-process mode: the scheduler must then treat that lane like a crashed node (`remove_node`, then reschedule).
   - Hybrid mode: optionally exit the worker process so xdist replaces it.
   - Tests must cover a thread stuck in `time.sleep`, in a blocking socket read, and in a pure-Python loop.
3. **Context propagation for child threads on Python 3.13 and earlier (F3).**
   - Opt-in `--lanes-propagate-context`: patch `threading.Thread.start` so the thread runs inside `contextvars.copy_context()` of the starter.
   - A test must show that stdout, logs and caplog records from a child thread are attributed to the right test.
4. **Static shared-state audit (F1).** `--lanes-detect` covers run time. Still open:
   - A `tools/audit_globals.py` AST scanner that reports `mock.patch`, `monkeypatch.setattr`/`setenv`/`chdir`, `os.environ[...] =`, `os.chdir`, `signal.signal`, `sys.path` mutation, `logging` level changes, and module-level mutable globals written from functions.
   - Output a CSV with file, line, pattern and a suggested action (`lanes_exclusive`, refactor, or safe). Do not auto-edit the user's tests.
5. **Failure-instrumentation plugin support.** Its design is on the `feature/lanes` branch of pytest-failure-instrumentation (`docs/pytest-lanes-support.md`); implementing it is open. Add a contract test that loads it under all three modes and diffs its output against plain `-n`.
6. **Scope-level work stealing (F7).** xdist's `worksteal` moves single tests, which would split an environment. If lanes still idle at the end of runs with the recommended `_reschedule` (README), add stealing of whole, not-yet-started scopes, with a parity test.
7. **CI.** `.github/workflows/ci.yml` mirrors `scripts/matrix.sh` and runs only by hand; add a schedule for the pytest/xdist `main` job if the user wants one. If CI is air-gapped, point `scripts/matrix.sh` at a local index (`UV_INDEX_URL`).
8. **Upstream issues.**
    - pytest: keep `PYTEST_CURRENT_TEST` out of the process environment for threaded runners (`putenv` while another thread spawns a subprocess breaks the spawn) and delete it with `pop(..., None)`; a public API for per-context SetupState and fixture caches (would remove P1, P2 and P6); `Cache.set` is not atomic (truncate, then write), racy across xdist workers too.
    - xdist: document the node protocol and add a lane-capable worker hook (would remove X1–X4); loadgroup silently ignores `xdist_group` marks added by a non-`tryfirst` `collection_modifyitems`; a worker's INTERNALERROR can leave the run exiting 0 (3.8.0); workers do not inherit the controller's `-X` options.

## Working rules

- **Test locally, not on GitHub runners: they are paid.** `scripts/matrix.sh` is the gate. `.github/workflows/ci.yml` runs only when dispatched by hand; don't add `push`/`pull_request` triggers without asking.
- Run `tests/` on at least two pytest versions before declaring anything done.
- When a plugin misbehaves under lanes, write the failing contract test first, then fix it.
- Add no new internal touchpoints unless unavoidable (see invariant 3). A new one goes in the module that owns its concern, with its check in `probes.py`.
- Keep `plugin.py` free of logic, and keep each touchpoint's patch and restore together in one context manager.
- Never set `report.node` to a `ThreadNode` in hybrid worker mode, because reports must stay serializable. Use `report.lane_id` instead.
- In hybrid mode, `--lanes` means lanes per process; the total is `-n` × `--lanes`. Don't change this silently.
- Never push a release tag (`vX.Y.Z`) without the user's go-ahead: it publishes to PyPI.

## Open questions for the user

1. Which Python version and build does the real suite run on (3.12, 3.13, 3.14, or free-threaded 3.14t)?
2. Is the environment derivable from the nodeid (a parametrize id), or does it come from a fixture or runtime lookup? If the latter, surface it via `ids=` or a `tryfirst` `xdist_group` mark.
3. Is CI air-gapped?

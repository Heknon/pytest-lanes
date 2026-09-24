# CLAUDE.md: pytest-lanes

Read this file first. Read DESIGN.md next for the full rationale, the evidence behind it, and the flags.

## What this is and why

We have a pytest suite of long, I/O-bound tests. Each test takes 1–2 hours, and we need to run thousands of them concurrently. pytest-xdist gives one process per concurrent test, at 150–500 MB each, so memory caps our concurrency. The suite's infrastructure is hundreds of thousands of lines of synchronous code, so converting it to async is not an option.

pytest-lanes runs pytest-xdist's own scheduler objects on thread "lanes":

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
| `compat.py` | Shims for third-party plugins that assume one test at a time per process | C1 |
| `integrity.py` | `Ledger`: the run-time integrity check. Each lane's routed hooks name the item it runs, replayed streams nest (logstart, reports, logfinish), each done item was logged, and (single process) every collected item ran as often as collected. `StdioWatch`: `sys.stdout`/`stderr`/`stdin` replaced while two or more lanes run tests (click's `CliRunner`) fails the run | |
| `probes.py` | Fail-closed startup checks, one function per touchpoint | all except P1, P5 |
| `detector/` | `--lanes-detect`, an isolated debugging tool, not used by lanes at run time: which tests change process-wide state. `walk.py` (read-only recursive snapshot), `sources.py` (process state, modules under the rootdir, plugin objects), `recorder.py` (live patch recording), `classify.py` (per-test / patched / grows / set-once), `report.py`, `plugin.py`. Only `SharedStateDetector` and `refuse_concurrent` are imported outside it | D1 |

Mode selection in `pytest_configure`:
- `config.workerinput` present: hybrid worker, `HybridWorkerSession`.
- `-n` or `--tx`: hybrid controller, `LanesController`.
- Otherwise: `SingleProcessSession`.

Other files:

- `tests/` is the spec: pytester subprocess tests (about 190) in `test_contract.py` (the original contract), `test_parity.py` (report parity in all modes), `test_robustness.py` (failure paths, options, run shapes), `test_isolation.py` (output, logging, basetemp, worker identity, warnings) `test_integrity.py` (the integrity check, and a canary suite under maximum thread-switching pressure), `test_patch_guard.py` (P14) and `test_detector.py` (`--lanes-detect`). Shared helpers live in `tests/lanes_testing.py`.
- `demo/` is a manual smoke test (see `demo/README.md`).
- `scripts/matrix.sh` runs the suite against several pytest/xdist versions, in separate venvs.
- `.github/workflows/ci.yml` is a draft CI workflow, manual-only (`workflow_dispatch`): GitHub runners are paid.

## Internal touchpoints

All are probed at startup (`probes.py`) except P1 and P5, which only the contract tests cover. That gap predates the module split; closing it is a candidate follow-up.

| # | Touchpoint | Purpose |
|---|---|---|
| P1 | `session._setupstate` | Router giving each lane its own `SetupState` |
| P2 | `FixtureDef.cached_result` / `._finalizers` / `.cached_param` | Class-level properties keyed per lane. `cached_param` is set and deleted by pytest's setuponly plugin (`--setup-show`/`--setup-only`); shared, one lane deleted it while another printed it (AttributeError, 3 of 5 runs on 3.14t) |
| P3 | `LoggingPlugin.caplog_handler` / `.report_handler` | Per-lane dispatch; a permanent root `_LogRouter` makes pytest's concurrent handler attach/detach harmless |
| P4 | pluggy `PluginManager._inner_hookexec` | Routes the 4 controller hooks to the main thread (the same slot pluggy's public `add_hookcall_monitoring` uses) |
| P5 | `item._nodeid` | `@group` suffix under loadgroup, identical to xdist's worker |
| P6 | `_pytest.runner._update_current_test_var` | pytest pops `PYTEST_CURRENT_TEST` without a default, so lanes finishing together raised KeyError (3 of 1,000). The replacement uses `del` + `suppress(KeyError)`, because `pop(k, None)` is check-then-delete and still races (constantly on 3.14t) |
| P7 | `config._tmp_path_factory` (`getbasetemp`, `_given_basetemp`, `_basetemp`), `config._tmpdirhandler._tmppath_factory` | A basetemp per lane, as xdist gives each worker: `<root>/ln3` (single process) or `<root>/popen-gw0.ln3` (hybrid), so `getbasetemp().parent` stays the run root shared by all workers. The process's own basetemp is created under a lock: pytest creates it lazily without one, and with `--basetemp` two lanes both `rmtree`+`mkdir`ed it. Needs lanes' `pytest_configure` to be `trylast` so the probe runs after the tmpdir plugin configures |
| P8 | `logging.Logger.manager.loggerDict` | pytest ≥ 9 iterates it whenever a test phase starts; a logger created on another lane at that moment raised "dictionary changed size" (reproduced 5/5 on 3.12 with 64 lanes). Replaced for the session by a dict subclass whose views come from an atomic copy |
| P9 | `_pytest.doctest.DoctestItem` | Doctest items run exclusively: doctest's runner swaps `sys.stdout` for the whole process, which captured other lanes' output and failed the doctest |
| P10 | `config.__class__` (a subclass with per-lane `workerinput`/`workeroutput` properties) | Each lane is its own xdist worker: `worker_id`, `testrun_uid`, `xdist.get_xdist_worker_id()` and `config.workerinput` name the lane (`ln3`, or `gw0.ln3` in hybrid, counting every lane of the run). The single-process main thread keeps the controller role (no `workerinput`). Probed: Config has no `__slots__`, and xdist still reads `config.workerinput` |
| P11 | `_pytest.recwarn.WarningsRecorder.__enter__` | Without context-aware warnings (Python < 3.14), `pytest.warns`/`deprecated_call`/`recwarn` swap process-wide warning state (5 of 6 concurrent blocks failed). A non-exclusive test entering one fails with instructions to mark it `lanes_exclusive`. Not installed when warnings are context-aware |
| P12 | `_pytest.cacheprovider.Cache.get` / `.set` | Serialized by one process-wide lock: pytest writes a value by truncating the file and then writing it, so a lane reading concurrently got the default (45 of 200 get-after-set tests failed) |
| P13 | stdlib `contextlib._RedirectStream.__enter__` / `__exit__` (`._stream`, `._new_target`) | `redirect_stdout`/`redirect_stderr` entered on a lane push the target on that lane's own stack instead of replacing `sys.stdout` for the process, which captured every other lane's prints (4 of 4 redirecting tests failed). Only while lanes capture (not with `-s`) |
| P14 | stdlib `unittest.mock._patch.__enter__` (`.getter`: a `partial(pkgutil.resolve_name, path)` for dotted paths, `.attribute`), `_patch_dict._patch_dict` (`.in_dict`); public `pytest.MonkeyPatch` methods | The patch guard: in a test that is not exclusive, a patch of a module or class attribute, anything by dotted path, `os.environ`/`sys.modules`, `setenv`/`delenv`, `chdir` or `syspath_prepend` fails the test with instructions. Session-scoped fixtures are guarded too (each lane tears its own down when it finishes, undoing the patch for lanes still running: seen as a KeyError); the message says to set such values in `pytest_configure`. Exempt: instances and classes defined in a function, pytest's own patches (its twisted support), `lanes_exclusive` and `lanes_allow_patches` tests, processes with one lane; `--lanes-allow-patches` / ini `lanes_allow_patches` turn it off |
| P15 | `CaptureManager.suspend_global_capture` / `resume_global_capture` (`_capture_fixture`, `suspend_fixture`, `resume_fixture`) | On a lane, they also suspend/resume the test's capture fixture. Plugins write to the terminal between the two (`--setup-show`); in plain pytest that bypasses capfd as a side effect of suspending global capture, which lanes turn off, so the line went into the test's capfd |
| C1 | pytest-rerunfailures ≥ 15: `config.failures_db` (`ClientStatusDB`) | A hybrid worker's lanes shared its one socket to the controller; interleaved request/response pairs killed a lane with `ValueError`. Its socket methods (`_get`, `_set`, `increment_suite_reruns`) are serialized |
| X1 | xdist scheduler protocol | Uses `add_node`, `add_node_collection`, `schedule`, `mark_test_complete`, `remove_node`, `tests_finished`, `collection_is_completed`, `numnodes`. Probed on each scheduler instance, never by import name: xdist 3.6.1 lacks `parse_tx_spec_config` |
| X2 | hybrid worker: `WorkerInteractor.channel`, `.sendevent`, `.item_index` | Located by class name, because xdist executes `remote.py` via execnet and `isinstance` fails |
| X3 | hybrid controller: `DSession.handle_crashitem` | Used for the 2nd and later crashed lanes of one worker |
| X4 | hybrid controller: `WorkerController.workerinput` / `workerinfo` / `workeroutput` | Mirrored on each `LaneProxy`, so a custom scheduler or plugin reading them sees a worker (`workerinput` gets the lane's id and the total lane count). Probed by checking the xdist code that sets them |
| D1 | `--lanes-detect` only: stdlib `unittest.mock._patch.__enter__` (and `.getter`/`.attribute`), `_patch_dict._patch_dict` / `._unpatch_dict`; `contextlib._RedirectStream` (to tell redirects from stdio swaps) | Records patches applied inside a test body, which no snapshot sees. Probed by `detector.recorder.check_d1` (a UsageError for `--lanes-detect` if it fails); never installed in a lanes run |

## How to run

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -e ".[test]"
.venv/bin/python -m pytest tests -q -p no:cacheprovider -p no:warnings -n 4   # ~190 tests, ~80s
scripts/matrix.sh                        # 3.12 3.13 3.14 3.14t x 3 pytest/xdist combos (needs PyPI)
RUNS=20 scripts/matrix.sh 3.14t          # repeat runs, one interpreter
```

`scripts/matrix.sh` uses uv. Use a uv new enough to know CPython 3.14 final (uv 0.8.x only has 3.14.0rc2); `uvx --from uv uv python install 3.14 3.14t` works if the installed uv is older.

Required flags and environment:

- On Python 3.13 or earlier, `-p no:warnings` is mandatory, because `catch_warnings` is not thread-safe there and the plugin refuses to run otherwise. On Python 3.14 or later, use `-X context_aware_warnings=1` (or `PYTHON_CONTEXT_AWARE_WARNINGS=1`) instead; free-threaded 3.14t has it on by default.
- pytest 8.3.5 and earlier also need `-p no:threadexception -p no:unraisableexception`, because those versions swap global hooks per test. `tests/` adds these flags automatically when needed.
- Tests use `runpytest_subprocess`. Keep it that way: in-process pytester would share the patched `FixtureDef` class and the global hooks.

## Verified status

- **Round 5 (review and challenge cycles):** two independent reviews, a 3.14t chaos suite (48 lanes and 2 × 24), a 4,000-test soak and a 500-lane scale run found 17 defects, including a per-test memory leak on pytest 9; all fixed test-first (DESIGN.md "Round 5"). Chaos suite 20/20 clean; suite green on 3.12 (pytest 8.0.2, 9.1.1) and 3.14t (9.1.1).
- **Round 4 (plugins, OS behaviour, silent failures):** a harness ran 55 scenarios under plain, `-n`, `--lanes` and hybrid (pytest-cov, asyncio, mock, repeat, check, order, dependency, randomly, timeout, env; fork, spawn, stdin, `sys.exit`, recursion, thread exceptions, cache, stepwise, live logging, unittest, subtests, Ctrl-C). Eight bugs fixed test-first (DESIGN.md "Found and fixed in round 4"). Suite green on 3.12 (pytest 8.0.2, 9.1.1) and 3.14t (8.3.5, 9.1.1).
- **Round 3 (edge-case sweep, 4-core container):** all tests pass on the full local matrix: CPython 3.12.3, 3.13.12, 3.14.7 and 3.14.7t, each with pytest 8.0.2 / xdist 3.6.1, 8.3.5 / 3.6.1 and 9.1.1 / 3.8.0. The only skips are the two 3.14-only warnings tests on 3.12/3.13. Twelve bugs and four policy gaps were fixed, each test-first (DESIGN.md "Found and fixed in round 3"). The instrumentation plugin's smoke run has identical report-log output in all three modes.
- **Round 2 (4-core container, uv):** all 18 tests pass (the 3.14 warnings test skips on 3.12/3.13) on CPython 3.12.3, 3.13.12, 3.14.7 and 3.14.7t, each with pytest 8.0.2 / xdist 3.6.1, pytest 8.3.5 / xdist 3.6.1 and pytest 9.1.1 / xdist 3.8.0.
  - The 15 original tests did **not** pass reliably on 4 cores: the P7 basetemp race failed the hybrid tests in roughly half of all runs on every version, and the P6 `pop` race failed 2–5 tests per run on 3.14t. Both are fixed, with contract tests.
- **Round 1 (Python 3.12, 1-core sandbox):** all 15 tests passed on pytest 8.0.2, 8.3.5 and 9.1.1, with xdist 3.6.1 and 3.8.0.
- **Scheduling:** the same custom scheduler works under `-n`, `--lanes`, and hybrid. Each environment is pinned to one lane, runs in order, and environments run in parallel.
- **Report parity:** report-log output is identical to plain xdist loadgroup, in both single-process and hybrid mode.
- **Crash recovery (hybrid):** a test calling `os._exit` was reported as crashed, xdist replaced the worker, and the work was rescheduled and passed.
- **Isolation:** per-test stdout, logs and caplog are attributed correctly. `-x` has exact per-lane semantics. rerunfailures works. capsys runs exclusively.
- **Scale:** 1,000 concurrent 3-second tests took 4.4s and 93 MB as 1 × 1000; 7.4s and 244 MB as 4 × 250; 14.5s and 848 MB as 20 × 50.

## Known issues (see DESIGN.md "Flags")

These are not bugs to "fix" by weakening the invariants.

- **Process-global state** in user tests and infrastructure: `mock.patch`, monkeypatch on shared modules, `os.environ`, `chdir`, signals. Patches through `mock`/pytest-mock/`monkeypatch` fail closed (P14, opt-out `--lanes-allow-patches`); direct assignments (`os.environ[k] = v`, `module.attr = x`) cannot be seen at run time: `--lanes-detect` finds them.
- **Warnings** before Python 3.14.
- **Child-thread output attribution** before Python 3.14.
- **Unkillable hung threads.**
- **Crash collateral:** every in-flight test in a crashed process is reported as crashed.
- **Exclusive tests pause their whole process.**
- **Unsupported:** `each` and `worksteal` modes, and `--pdb`.
- **Ctrl-C** interrupts every lane and runs its teardown, as pytest does; a lane blocked in one long C call (a sleep, a socket read without timeout) is abandoned after `lanes_interrupt_grace` (30s) and named. SIGTERM kills the process without teardown, in every mode.
- **Process-wide setters and stdio swaps:** `random.seed`, `socket.setdefaulttimeout`, `locale.setlocale`, `os.umask` etc. are shared by all lanes (`--lanes-detect` reports them); replacing `sys.stdout` (click's `CliRunner`) fails the run unless the test is `lanes_exclusive`. `contextlib.redirect_stdout` is per lane.
- **Wall-clock assertions** (`r.duration < N`) in `tests/` are load-sensitive (backlog item 2). The earlier unreproduced flake was most likely the P7 basetemp race, which a 1-core box rarely hits.
- **Round-3 findings still open (DESIGN.md F11–F16):**
  - `warnings.catch_warnings` used directly (not via pytest.warns) on Python ≤ 3.13;
  - per-test timeouts: pytest-timeout (single-process) and `faulthandler_timeout` are refused until `--lanes-timeout` (backlog 3);
  - `signal.signal` fails in lanes;
  - worker-side `logreport` consumers see reports late;
  - per-item overhead on GIL builds.

## Backlog, in priority order, with acceptance criteria

1. **Run on the target interpreters.** *Done in round 2: see Verified status; fixes P6 (atomic delete) and P7 (new).*
   - Add Python 3.13, 3.14 and 3.14t to the matrix and make it pass.
   - On 3.14, the warnings probe must accept `-X context_aware_warnings=1`.
   - Add a contract test proving per-test warning capture and `filterwarnings("error")` work under lanes on 3.14.
2. **Fix timing flakiness.**
   - Replace wall-clock assertions (`r.duration < N`) with overlap assertions: record start and end timestamps per test and assert that the expected tests overlapped in time.
   - The suite must pass 20 times in a row under load.
3. **Runner-level per-test timeout (F4).**
   - Add a `--lanes-timeout` option and a `lanes_timeout` marker.
   - On expiry: report the test failed with a stack dump of that lane's thread; try `PyThreadState_SetAsyncExc` as a best effort; if the thread doesn't return within a grace period, mark the lane dead and abandon it.
   - Single-process mode: the scheduler must then treat that lane like a crashed node (`remove_node`, then reschedule).
   - Hybrid mode: optionally exit the worker process so xdist replaces it.
   - Tests must cover a thread stuck in `time.sleep`, in a blocking socket read, and in a pure-Python loop.
4. **Context propagation for child threads on Python 3.13 and earlier (F3).**
   - Opt-in `--lanes-propagate-context`: patch `threading.Thread.start` so the thread runs inside `contextvars.copy_context()` of the starter.
   - A test must show that stdout, logs and caplog records from a child thread are attributed to the right test.
5. **Shared-state audit tool (F1).** *Runtime part done: `--lanes-detect` (the `detector/` package, `tests/test_detector.py`). Checked on pytest-failure-instrumentation, where it found the per-test state that its lane-support design names. Static part still open:*
   - A `tools/audit_globals.py` AST scanner that reports `mock.patch`, `monkeypatch.setattr`/`setenv`/`chdir`, `os.environ[...] =`, `os.chdir`, `signal.signal`, `sys.path` mutation, `logging` level changes, and module-level mutable globals written from functions.
   - Output a CSV with file, line, pattern and a suggested action (`lanes_exclusive`, refactor, or safe).
   - Run it on the user's repo and hand over the report. Do not auto-edit the user's tests.
6. **Crash collateral (F5).** In hybrid mode, annotate sibling-lane crash reports as collateral. Use the `pytest_handlecrashitem` hook or a report attribute, so dashboards can tell the culprit from the victims. Parity must be kept for the culprit's report.
7. **worksteal support.** Implement `send_steal` and the unscheduled round trip for `ThreadNode`, and for `LaneProxy` via a new `lanes_steal` command. Add a contract test with a worksteal parity check.
8. **User's failure-instrumentation plugin.**
   - Ask the user for its location.
   - Classify each of its hookimpls: A (report consumer), B (xdist observer), C (process protocol), or D (execution-side).
   - Add a contract test that loads it under all three modes and diffs its output against plain `-n`.
   - For class D, fix it by moving any "current test" global to `item.stash` or a contextvar.
9. **CI.** Make `.github/workflows/ci.yml` actually run, including the nightly job against pytest and xdist `main`. If the user's environment is air-gapped, adapt `scripts/matrix.sh` to a local package index instead.
10. **Upstream.** Draft two issues:
    - pytest: `PYTEST_CURRENT_TEST` should use `pop(..., None)`, plus a public API for per-context SetupState and fixture caches, to remove P1, P2 and P6.
    - xdist: document the node protocol and add a lane-capable worker hook, to remove X1–X4. Also report that loadgroup silently ignores `xdist_group` marks added by a non-`tryfirst` `collection_modifyitems`: verified with 3 workers, where one group's tests landed on 3 different workers. And that a worker's INTERNALERROR is printed by the controller but the run can still exit 0 (verified on xdist 3.8.0: "3 passed", exit 0), and that workers do not inherit the controller's `-X` options (`context_aware_warnings`), only environment variables.
    - pytest: `Cache.set` is not atomic (truncate, then write): under plain `-n 4`, 6–9 of 300 concurrent get-after-set tests read the default. Lanes serialize it within a process (P12); across processes it stays as racy as xdist.

## Working rules

- **Test locally, not on GitHub runners: they are paid.** `scripts/matrix.sh` is the gate. `.github/workflows/ci.yml` runs only when dispatched by hand; don't add `push`/`pull_request` triggers without asking.

- Run `tests/` on at least two pytest versions before declaring anything done.
- When a plugin misbehaves under lanes, write the failing contract test first, then fix it.
- Add no new internal touchpoints unless unavoidable (see invariant 3). A new one goes in the module that owns its concern, with its check in `probes.py`.
- Keep `plugin.py` free of logic, and keep each touchpoint's patch and restore together in one context manager.
- Never set `report.node` to a `ThreadNode` in hybrid worker mode, because reports must stay serializable. Use `report.lane_id` instead.
- In hybrid mode, `--lanes` means lanes per process; the total is `-n` × `--lanes`. Don't change this silently.
- Don't reintroduce a fork of pytest-threadpool. DESIGN.md explains why.

## Questions to ask the user before starting backlog items 3–8

1. Which Python version and build does the real suite run on (3.12, 3.13, 3.14, or free-threaded 3.14t)?
2. Where is the failure-instrumentation plugin, and which hooks does it implement?
3. Is the environment derivable from the nodeid (a parametrize id), or does it come from a fixture or runtime lookup? If the latter, surface it via `ids=` or a `tryfirst` `xdist_group` mark.
4. Is CI air-gapped?

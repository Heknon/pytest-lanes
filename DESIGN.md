# pytest-threadlanes: xdist schedulers on thread lanes, in one process or many

## Three modes, one scheduler

```python
# conftest.py: the only scheduling code you write
from xdist.scheduler import LoadScopeScheduling

class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid):              # 'test_x.py::test_step[envB-2]' -> 'envB'
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]

    def _reschedule(self, node):                 # recommended: see README "Scheduling"
        if node.shutting_down or not self.workqueue or self._pending_of(self.assigned_work[node]) <= 1:
            super()._reschedule(node)

def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)            # don't gate on config.getvalue("dist")
```

`_reschedule` is optional but recommended for long tests: xdist's loadscope otherwise gives a node its next environment when the current one is down to two tests, so it can wait behind both while other lanes sit idle. It cannot wait for zero: a worker starts its last queued test only once it knows the next (for fixture teardown). `tests/test_contract.py` runs this code verbatim in all three modes.

| Command | Layout | Scheduler sees |
|---|---|---|
| `pytest -n 8` | 8 processes (plain xdist) | 8 nodes |
| `pytest --lanes 200` | 1 process × 200 thread lanes | 200 `ThreadNode`s |
| `pytest -n 8 --lanes 25` | 8 xdist processes × 25 lanes each (200 total) | 200 `LaneProxy` virtual nodes |

The same `EnvScheduling` instance logic runs in all three modes. An environment is pinned to one lane, runs sequentially, and environments run in parallel across all lanes in all processes.

## Architecture

### Single process (`--lanes M`)

- The main thread plays xdist's controller: `add_node`, `add_node_collection`, `schedule`, `mark_test_complete`, and shutdown once `tests_finished` is true.
- Each `ThreadNode` implements the four members xdist's `load*` schedulers use (`gateway`, `shutting_down`, `send_runtest_some()`, `shutdown()`). It runs xdist's worker loop: lookahead to find `nextitem`, which drives teardown, until a SHUTDOWN marker arrives.
- Report hooks are replayed on the main thread with `report.node` set to the lane. This follows xdist's controller/worker hook split exactly. The one exception is pytest's own `Session.pytest_runtest_logreport`, which counts failures for `-x`/`--maxfail`: it runs on the lane, under a lock, as it runs inside an xdist worker, so the failing test is torn down fully and the lane stops by itself.

### Hybrid (`-n P --lanes M`)

**Controller.** This is the real xdist `DSession`, with real worker processes, crash detection and worker restart. Our `LaneMux` wraps whatever scheduler `pytest_xdist_make_scheduler` returns, using a new-style hook wrapper on that public hook.
- Toward DSession, the mux presents the P real workers.
- Toward the scheduler, it presents P×M `LaneProxy` virtual nodes.
- `LaneProxy.send_runtest_some()` becomes `wc.sendcommand("lanes_runtests", lane=..., indices=...)`.
- `mark_test_complete` is routed back to the owning lane.

**Worker.** Each worker process takes over xdist's worker loop, installs its own channel callback, and runs M `ThreadNode`s. After each item it sends xdist's normal `runtest_protocol_complete` event. Reports travel through xdist's own serialization.

The result is that process-level xdist protocol plugins (pytest-cov, pytest-metadata) see an ordinary xdist run. Each report carries a serializable `report.lane_id` such as `"gw3.ln7"`.

## Private touchpoints

Touchpoints are probed at startup (`probes.py`), and the plugin fails closed if a probe fails. P1 and P5 have no startup probe yet; the contract tests cover them. Each patch lives in the module named in CLAUDE.md's file map, with its install and restore in one context manager.

| # | Touchpoint | Why |
|---|---|---|
| P1 | `session._setupstate` | Gives each lane its own SetupState |
| P2 | `FixtureDef.cached_result` / `_finalizers` / `cached_param` | Per-lane fixture caches, and the param pytest's `--setup-show` keeps on the FixtureDef |
| P3 | `LoggingPlugin.caplog_handler` / `report_handler` | Per-lane log capture |
| P4 | pluggy `_inner_hookexec` | Routes controller hooks to the main thread |
| P5 | `item._nodeid` | Adds the `@group` suffix under loadgroup, as xdist's worker does |
| P6 | `_pytest.runner._update_current_test_var`; `os.environ.__class__` | `PYTEST_CURRENT_TEST`, `PYTEST_XDIST_WORKER` and `_COUNT` per lane, kept out of the process environment |
| P7 | `config._tmp_path_factory` / `_tmpdirhandler` | A basetemp per lane, like xdist's per-worker basetemp; the process's basetemp is created under a lock |
| P8 | `logging.Logger.manager.loggerDict` | pytest ≥ 9 iterates it at every test phase; views are served from a copy so concurrent logger creation cannot break that |
| P9 | `_pytest.doctest.DoctestItem` | Doctests run exclusively, because doctest swaps `sys.stdout` for the whole process |
| P10 | `config.__class__` (per-lane `workerinput`/`workeroutput`) | Each lane is its own xdist worker for `worker_id`, `testrun_uid` and `xdist.get_xdist_worker_id()` |
| P11 | `WarningsRecorder.__enter__` | Before 3.14: `pytest.warns`/`deprecated_call`/`recwarn` in a non-exclusive test fail with instructions |
| C1 | pytest-rerunfailures `ClientStatusDB` | Hybrid only; its one per-worker socket is serialized across lanes |
| C2 | pytest-rerunfailures ≥ 16 `suspended_finalizers` | Per lane: a test about to be rerun parks its setup stack there, and another lane's teardown took it |
| X1 | xdist scheduler protocol | Semi-public; probed on each scheduler instance |
| X2 | `WorkerInteractor.channel` / `.sendevent` / `.item_index` | Hybrid mode only |
| X3 | `DSession.handle_crashitem` | Hybrid mode only; reports the 2nd and later crashed lanes of one worker |
| X4 | `WorkerController.workerinput` / `workerinfo` / `workeroutput` | Hybrid mode only; mirrored on each `LaneProxy` so custom schedulers see worker-shaped nodes |
| P12 | `_pytest.cacheprovider.Cache.get` / `.set` | Serialized per process: a concurrent read saw a half-written value |
| P13 | stdlib `contextlib._RedirectStream.__enter__` / `__exit__` | `redirect_stdout`/`redirect_stderr` redirect only the lane that entered them |
| P14 | stdlib `unittest.mock._patch.__enter__`, `_patch_dict._patch_dict`; `pytest.MonkeyPatch`; audit events `os.putenv`/`os.unsetenv`/`os.chdir` (public) | The patch guard: a process-wide patch or direct environment/cwd write in a test that is not exclusive fails at once |
| P15 | `CaptureManager.suspend_global_capture` / `resume_global_capture` | On a lane they also suspend the test's capture fixture, so a plugin's terminal output (`--setup-show`) is not captured by the test's capfd |
| D1 | stdlib `unittest.mock._patch.__enter__`, `_patch_dict._patch_dict` / `_unpatch_dict`, `contextlib._RedirectStream` | `--lanes-detect` only (never in a lanes run); records patches made inside a test body |

## Correctness defences

A race that crashes gets noticed. The dangerous kind leaves a run green while a report, an output or a fixture value belongs to another test. Three defences:

1. **Run-time integrity check, in every run (`integrity.py`).** On each lane, every routed `logstart`/`logreport`/`logfinish` must name the item that lane is running. On the main thread, each lane's replayed stream must nest: logstart, that item's reports, logfinish. When a lane reports an item done, the replay must just have shown its logstart and logfinish; this is exact, because a lane's hook calls precede its `ItemDone` in one FIFO queue and the lane waits for its acknowledgement. In single-process mode, a run that was not stopped must have run every collected item as often as it was collected. `StdioWatch` fails the run if `sys.stdout`/`stderr`/`stdin` is replaced while two or more lanes run tests. Any violation fails the run with INTERNALERROR (exit 3), naming what was wrong.
   - xdist's controller prints a worker's INTERNALERROR and the run can still exit 0 (xdist 3.8.0). The hybrid controller sets the exit status to INTERNALERROR whenever a worker reports one, so a failed check in a worker cannot look green.
2. **A canary suite under maximum switching pressure (`tests/test_integrity.py`).** 200 tests on 48 lanes (and 2 × 24) with `sys.setswitchinterval(1e-6)`. Each test checks that its fixtures of every scope, `worker_id`, `tmp_path` and caplog records are its own, and prints, logs and writes to stderr a token 30 times. Every section of every report must hold only its test's token, exactly 30 times, and the report-log must equal plain xdist's. `RUNS=N scripts/matrix.sh` repeats it to hunt rare races.
3. **Guards that fail closed on process-wide changes:** the patch guard (P14) for patches, environment writes and `chdir`; the `pytest.warns` guard before Python 3.14 (P11); capture fixtures, pytest-cov's `no_cover` and doctests run exclusively.

What none of these can see is process-global state that tests or their infrastructure change directly (F1). `--lanes-detect` finds it ahead of time.

## `--lanes-detect`

A sequential run (it refuses `--lanes` and `-n`: concurrent tests would make every snapshot ambiguous) that reports which tests and fixtures change process-wide state.

- **Snapshots.** Before setup (A), after the call phase with fixtures still active (B), after teardown (C). They cover process state (environment, cwd, `sys.path`, logging levels, signal handlers) and a recursive walk from the globals of every module under the rootdir (plus `lanes_detect_modules`), the class attributes of classes they define, and every registered plugin object except pytest's, pluggy's, xdist's and this plugin's. An object is expanded once per snapshot, at the first path that reaches it, which bounds the walk and makes cycles safe; a path absent from a snapshot that did not look there (another path expanded the object, or the node cap was reached) is not compared.
- **Read-only.** Types are read with `type()`, never `isinstance()` (which reads `__class__`, as proxies and spec'd mocks answer); attributes come from `__dict__` and `__slots__`, so no property, `__getattr__` or `__eq__` runs; containers are read through their base type's C methods; strings are kept as length and hash only.
- **Fixtures.** Session, package, module and class fixtures are snapshotted around their setup and teardown and reported under their own name (`fixture <name> (<scope> scope)`), never against the test that happened to run them.
- **Live recording** (D1 and public APIs): `mock.patch`/`patch.dict` (so pytest-mock), `pytest.MonkeyPatch`, process-wide setters (`random.seed`, `socket.setdefaulttimeout`, ...), `sys.stdout`/`stderr`/`stdin` swaps, and the `os.putenv`/`os.unsetenv`/`os.chdir` audit events. Patch targets are labelled by the path the walk reached them at, so one `lanes_detect_ignore` pattern covers a patch and an assignment of the same thing.
- **Classification.** Changed and restored within a test, or changed in two or more: `per-test` (unsafe). Patched: `patched` (unsafe). A container growing in two or more tests: `grows` (check). Changed once, then stable: `set-once` (a cache; fine if thread-safe).

Limits: a value set and restored inside one test body is not seen unless it goes through mock or monkeypatch; state inside C extensions; objects deeper than `lanes_detect_max_depth`; objects of packages not inspected (compared by identity only); external resources (ports, files, database rows), which collide under plain xdist too.

## Verification

- **The contract suite (`tests/`, about 300 pytester subprocess tests):** scheduling, report parity against plain `-n` in every mode, isolation (output, logging, basetemp, worker identity and environment, warnings), robustness (failure paths, `-x`, Ctrl-C, crashes, options), the integrity check and canary suite, the patch guard, and the detector. Every fix started with a test that failed without it.
- **The matrix (`scripts/matrix.sh`):** CPython 3.12, 3.13, 3.14 and free-threaded 3.14t, each with pytest 8.0.2 / xdist 3.6.1, 8.3.5 / 3.6.1 and 9.1.1 / 3.8.0.
- **Stress:** the canary suite 20 times on 3.14t; a replica of a long-running environment suite (leased environments, session and module fixtures, subprocesses, child-thread logging, rerunfailures) at up to 64 environments under `--lanes 64` and `-n 4 --lanes 16`, with a crash, `-x` and Ctrl-C injected, identical to xdist; a 6,000-test soak on 200 lanes, whose memory grows exactly as plain pytest's (pytest keeps every report for the summary); 1,000 concurrent tests in one process.
- **Plugins:** terminal, junitxml, report-log, pytest-html, pytest-metadata, pytest-cov, rerunfailures, pytest-mock, pytest-timeout (hybrid), and some 50 more scenarios (asyncio, repeat, order, dependency, randomly, subtests, unittest, fork and spawn, stdin, `sys.exit`, cache, stepwise, live logging).

## Sizing: processes × lanes

Measured on a 1-core sandbox, running 1,000 trivial I/O tests (3s each):

| Layout | Wall time | Peak RSS, all processes |
|---|---|---|
| 1 × 1000 | 4.4s | 93 MB |
| 4 × 250 | 7.4s | 244 MB |
| 20 × 50 | 14.5s | 848 MB |

Each process costs one interpreter plus a full collection. That was about 40 MB here; for a suite with heavy infrastructure it is 150–500 MB. The extra wall time is interpreter startup on one core, and on a multi-core machine it would largely overlap.

So choose P for isolation, and use lanes for throughput. Reasons to raise P:

- **Crash blast radius.** One segfault or OOM ends every in-flight test in that process, so up to M−1 innocent tests are reported as crashed, as xdist reports its one crashed test. What happens next is the scheduler's (F5): under a loadscope-based scheduler (the recommended `EnvScheduling`) each of them is run again on the replacement worker, from the step that was running; under `--dist load` they are not. Either way, one crash costs up to M−1 hours of work in flight.
- **Hang containment.** Threads can't be killed, but a process can. An external watchdog that kills a wedged worker lets xdist replace it and reschedule the work. This is the practical answer to hung tests.
- **GIL headroom.** If each test's infrastructure spends a fraction c of its time on CPU, one process saturates at roughly 1/c concurrent lanes. For example, 2% CPU per test caps out around 50 lanes per core.
- **Per-lane fixture memory,** which is multiplied by M within each process.

Reasons to lower P: base memory per process (P × 150–500 MB for heavy infrastructure) and collection time per process.

A reasonable starting point is 8–16 processes × 25–50 lanes. Then adjust using measured CPU and memory per process.

## Plugin compatibility

| Class | Single-process lanes | Hybrid |
|---|---|---|
| A: report consumers (terminal, junitxml, report-log, html) | Work | Work |
| B: xdist observers (`report.node`, node hooks) | `report.node` is the lane; node hooks only via opt-in allowlist | Real xdist node hooks (per process); `report.lane_id` gives the lane |
| C: xdist protocol users (pytest-cov, pytest-metadata) | Run in single-process mode | See a normal xdist run |
| D: execution-side plugins | Run concurrently in threads, so must be thread-safe | Same |

## Limitations

| # | Issue |
|---|---|
| F1 | **Process-global state is the biggest risk:** `mock.patch`, `monkeypatch` on shared modules, `os.environ`, `chdir`, signals, `caplog.set_level`. Run `--lanes-detect`, and mark affected tests `lanes_exclusive` or make the state per lane. |
| F2 | **Warnings:** `catch_warnings` isn't thread-safe before Python 3.14. Use `-X context_aware_warnings=1` on 3.14+ (the default on 3.14t), or `-p no:warnings`, which makes `filterwarnings` marks inert. |
| F3 | **Output from threads your tests spawn** is only attributed with `-X thread_inherit_context=1` on Python 3.14. fd-level writes are never attributed per test. |
| F4 | **Hung tests:** threads can't be killed. In hybrid mode, use a process watchdog. In single-process mode there's no answer yet. |
| F5 | **Crash collateral:** in hybrid mode, every in-flight test of a crashed process is reported crashed, as xdist reports its crashed test, because which lane crashed it cannot be told apart. What happens next is the scheduler's: under a loadscope-based scheduler (loadscope, loadgroup, `EnvScheduling`) the unfinished environments are requeued whole, starting at the step that was running, so each of those tests is run again, in order, on one lane. Under `--dist load` they are not rerun, a sibling lane's next queued test is reported crashed and dropped, and a test whose reports were sent but whose completion was not is reported crashed after passing and run again |
| F6 | **Exclusive tests pause lanes.** In hybrid mode an exclusive test waits for, and then blocks, every lane in its process; with hour-long tests that can drain the process for hours. Keep `capsys`/`capfd`/`recwarn` tests out of long suites, or run them in a separate plain invocation. |
| F7 | **`each` and `worksteal` modes are unsupported.** xdist's `worksteal` moves single tests, which would split an environment; only stealing whole, not-yet-started scopes would suit environment suites. `--pdb` is unsupported, as under xdist: debug a test by running it without `--lanes` |
| F8 | **Ctrl-C** interrupts every lane and runs its teardown; a lane blocked in one long C call cannot be interrupted and is abandoned after `lanes_interrupt_grace`, named. SIGTERM kills the process without teardown, as it does plain pytest |
| F9 | **`--lanes` means lanes per process in hybrid mode.** The total is `-n` × `--lanes`. |
| F17 | **`PYTEST_CURRENT_TEST` in subprocesses.** In-process it is per lane (P6). It is not in the process environment, so a subprocess started without `env=` does not see it (under xdist it would); pass `env=os.environ.copy()` |
| F11 | **`PYTEST_XDIST_WORKER` in subprocesses.** In-process, `os.environ["PYTEST_XDIST_WORKER"]` and `_COUNT` name the lane (P6), like `worker_id` (P10). A subprocess started without `env=` sees the process's value (the worker's, `gw0`, in hybrid mode; none in single-process mode); pass `env=os.environ.copy()` |
| F12 | **Warning capture before Python 3.14** is process-wide: `pytest.warns`, `deprecated_call` and `recwarn` fail closed in a test that is not `lanes_exclusive` (P11). `warnings.catch_warnings` used directly, in tests or libraries, cannot be guarded. On 3.14+ with context-aware warnings all of these are safe |
| F13 | **Per-test timeouts.** In single-process mode pytest-timeout would fall back to its thread method, which `os._exit`s the whole process on a timeout, so it is refused there (option, ini, `PYTEST_TIMEOUT` or marker); it works in hybrid mode, where xdist replaces the killed worker. `faulthandler_timeout` is refused in both lane modes, because its process-wide timer never fires for the hung test |
| F14 | **`signal.signal` in a test raises `ValueError`** under lanes: Python allows it on the main thread only. |
| F15 | **Worker-side `pytest_runtest_logreport` consumers see reports late.** In xdist a conftest's implementation also runs synchronously in the worker; under lanes it runs only on the main thread, after the test's teardown. For example, a fixture teardown that reads what a conftest `logreport` recorded finds nothing. Use `pytest_runtest_makereport`, which runs on the lane. |
| F18 | **A thread pool shared between tests** (a module-level `ThreadPoolExecutor`) keeps, on Python 3.14 with `thread_inherit_context`, the lane context of the test that started its threads: a later test's task run there has its output, logs, `PYTEST_CURRENT_TEST` and patch-guard decisions attributed to that first lane. Submit with `contextvars.copy_context().run`, or use a pool per test. Before 3.14 such output is unattributed (F3). `repr(os.environ)`, `os.environb` and pickling do not show the per-lane values of P6. |
| F16 | **Per-item overhead on GIL builds.** Each item waits for the main thread to replay its reports. With many CPU-busy lanes the main thread rarely gets the GIL: 3,000 tiny tests took 13s on 1 lane but 108s on 4 lanes and about 200s on 20–200 lanes on 3.12 (7s on 3.14t). It is negligible for long I/O-bound tests. |

## Compatibility plan

- Feature probes, fail-closed.
- A CI matrix: the oldest supported pytest/xdist, the current releases, and pytest plus xdist `main`.
- Upper-bound pins, raised only after the contract suite passes.
- Upstream candidates: a public per-context SetupState and fixture cache in pytest (would remove P1, P2 and P6); keeping `PYTEST_CURRENT_TEST` out of the process environment for threaded runners; an atomic `Cache.set`; a documented node protocol plus a lane-capable worker hook in xdist (would remove X1–X4); and a worker's INTERNALERROR failing an xdist run.

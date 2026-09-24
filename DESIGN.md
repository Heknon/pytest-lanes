# pytest-lanes: xdist schedulers on thread lanes, in one process or many

## Three modes, one scheduler

```python
# conftest.py: this is the only scheduling code you write
from xdist.scheduler import LoadScopeScheduling

class EnvScheduling(LoadScopeScheduling):
    def _split_scope(self, nodeid):              # 'test_x.py::test_step[envB-2]' -> 'envB'
        return nodeid.rsplit("[", 1)[1].split("-", 1)[0]

def pytest_xdist_make_scheduler(config, log):
    return EnvScheduling(config, log)            # don't gate on config.getvalue("dist")
```

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
| P2 | `FixtureDef.cached_result` / `_finalizers` | Per-lane fixture caches |
| P3 | `LoggingPlugin.caplog_handler` / `report_handler` | Per-lane log capture |
| P4 | pluggy `_inner_hookexec` | Routes controller hooks to the main thread |
| P5 | `item._nodeid` | Adds the `@group` suffix under loadgroup, as xdist's worker does |
| P6 | `_pytest.runner._update_current_test_var` | Fixes a `PYTEST_CURRENT_TEST` race (see below) |
| P7 | `config._tmp_path_factory` / `_tmpdirhandler` | A basetemp per lane, like xdist's per-worker basetemp; the process's basetemp is created under a lock |
| P8 | `logging.Logger.manager.loggerDict` | pytest ≥ 9 iterates it at every test phase; views are served from a copy so concurrent logger creation cannot break that |
| P9 | `_pytest.doctest.DoctestItem` | Doctests run exclusively, because doctest swaps `sys.stdout` for the whole process |
| P10 | `config.__class__` (per-lane `workerinput`/`workeroutput`) | Each lane is its own xdist worker for `worker_id`, `testrun_uid` and `xdist.get_xdist_worker_id()` |
| P11 | `WarningsRecorder.__enter__` | Before 3.14: `pytest.warns`/`deprecated_call`/`recwarn` in a non-exclusive test fail with instructions |
| C1 | pytest-rerunfailures `ClientStatusDB` | Hybrid only; its one per-worker socket is serialized across lanes |
| X1 | xdist scheduler protocol | Semi-public; probed on each scheduler instance |
| X2 | `WorkerInteractor.channel` / `.sendevent` / `.item_index` | Hybrid mode only |
| X3 | `DSession.handle_crashitem` | Hybrid mode only; reports the 2nd and later crashed lanes of one worker |
| X4 | `WorkerController.workerinput` / `workerinfo` / `workeroutput` | Hybrid mode only; mirrored on each `LaneProxy` so custom schedulers see worker-shaped nodes |
| D1 | stdlib `unittest.mock._patch.__enter__`, `_patch_dict._patch_dict` / `_unpatch_dict` | `--lanes-detect` only (never in a lanes run); records patches made inside a test body |

## Verified

The contract suite has 18 tests. It passes on CPython 3.12, 3.13, 3.14 and free-threaded 3.14t, each with pytest 8.0.2 / xdist 3.6.1, 8.3.5 / 3.6.1 and 9.1.1 / 3.8.0 (round 2, 4 cores). The older pytest versions need two plugins disabled, as explained below.

- **Same custom scheduler under `-n 3` and `--lanes 3`:** each environment ran sequentially on one worker, and environments ran in parallel.
- **Hybrid `-n 2 --lanes 3`:** 6 environments × 3 steps each. Every environment stayed on one lane in one process, in order, and both processes were used. Wall time was 2.5s, against 1.8s of sequential work per environment.
- **Report parity:** report-log output is identical to plain `-n 2 --dist loadgroup` in both single-process and hybrid mode. That covers nodeids with the `@group` suffix, outcomes and captured sections.
- **Hybrid crash handling:** a test called `os._exit(1)`. xdist reported the crashed test, replaced the worker and rescheduled the requeued scopes, and every test passed on the new worker.
- **Hybrid exclusivity:** a `capsys` test ran exclusively inside its worker, protected by a read/write lock, while noisy tests ran in other lanes.
- **Semantics carried over from earlier rounds:** fixture isolation, per-test stdout and log capture, `-x`, rerunfailures, and allowlisted node hooks.

### Found and fixed in round 1

1. **`PYTEST_CURRENT_TEST` race in pytest core.** The runner calls `os.environ.pop("PYTEST_CURRENT_TEST")` without a default. When two lanes finish at the same moment, one of them gets a KeyError during teardown. This showed up in 3 of 1,000 tests on a 40×25 run. It affects single-process lanes too; it just hadn't triggered there yet. Fixed via P6. The variable's value is now "last writer wins", which is only meaningful per lane.
2. **xdist's worker reads `self.item_index` when forwarding each report.** Lane reports are replayed with the matching index set (X2).
3. **pytest 8.3.5 and earlier swap `threading.excepthook` and `sys.unraisablehook` per test phase.** Under lanes these swaps race. pytest 9.1.1 installs them once instead. On older pytest the plugin refuses to run unless you pass `-p no:threadexception -p no:unraisableexception`. With those disabled, unhandled thread exceptions still print, but they no longer become per-test warnings.

### Found and fixed in round 2 (target interpreters, 4 cores)

The 15-test suite had only ever run on a 1-core sandbox. On a 4-core container it failed in about half of all runs, on every pytest version, and 2–5 tests per run on free-threaded 3.14t. Both causes are lanes concurrency bugs, not timing:

1. **basetemp creation race (new touchpoint P7).** `TempPathFactory.getbasetemp()` creates the base temp directory on first use, without a lock. With `--basetemp`, which xdist always sets on its workers, two lanes that request `tmp_path` or `tmp_path_factory` at the same time both `rm_rf` and `mkdir` it, and one fails with `FileExistsError`. It affects single-process lanes as well. The fix wraps `getbasetemp` on the process's factory instance with a lock, so creation stays lazy and exactly as pytest does it. The probe needs `config._tmp_path_factory` to exist, so lanes' `pytest_configure` is now `trylast` (after the builtin tmpdir plugin). This was very likely the "unreproduced intermittent failure" from round 1.
2. **The P6 fix still raced.** `os.environ.pop(key, None)` is `MutableMapping.pop`, which reads the key and then deletes it, so another lane can delete in between. It is now `del os.environ[key]` inside `suppress(KeyError)`. On 3.14t this hit several times per suite run.
3. **Warnings on 3.14.** With `context_aware_warnings` (the default on 3.14t, `-X context_aware_warnings=1` on 3.14), pytest's own per-test `catch_warnings` works under lanes as-is: `filterwarnings("error")` and `("ignore")` stay inside their test, and `pytest_warning_recorded` output matches `-n`. No plugin change was needed. A contract test proves it, and it fails when the probe is bypassed on a non-context-aware interpreter.

### Found and fixed in round 3 (edge-case sweep)

An adversarial sweep ran about 60 scenarios under `-n`, `--lanes` and hybrid, and compared outcomes, exit codes and report-log. Each confirmed bug got a contract test first (`tests/test_robustness.py`, `test_isolation.py`, `test_parity.py`):

1. **`-x`/`--maxfail` with a failing session-fixture teardown gave INTERNALERROR.** pytest tears the failing test down fully once `session.shouldfail` is set, but that flag is set by `Session.pytest_runtest_logreport`, which lanes replayed later. That implementation now runs on the lane, as in an xdist worker; the exit code is 2, as xdist's.
2. **pytest ≥ 9 logger race (new touchpoint P8).** `catching_logs` iterates `loggerDict` at every test phase; a logger created on another lane made it raise. Seen on 3.14t, and reproduced 5/5 on 3.12.
3. **Doctests stole other lanes' output (new touchpoint P9).** doctest swaps `sys.stdout` for the whole process, so doctests now run exclusively.
4. **Capture:**
   - `-s` still captured.
   - Bytes written to `sys.stdout.buffer` escaped capture.
   - `capsys`/`capfd` requested via `getfixturevalue` ran unguarded. It now fails with instructions.
5. **Shared basetemp (P7).** A per-lane session fixture could not `mktemp` a fixed name. Each lane now has its own basetemp, and `getbasetemp().parent` is still the run root.
6. **pytest-rerunfailures ≥ 15 in hybrid mode (new touchpoint C1).** A worker's lanes shared the plugin's one socket to the controller, so exchanges interleaved and a lane died. Found by the version matrix, not the first sweep.
7. **Options:**
   - Collection errors aborted single-process runs, whereas xdist runs the rest.
   - `--trace` hung.
   - `--lanes -1` gave an unrelated error.
   - `--dist` was ignored without `-n`.

Verified to match xdist, and now locked in by tests: every outcome kind under all four dist modes (including unittest, doctests, xfail/xpass/strict, setup and teardown errors, and odd parametrize IDs), junitxml, rerunfailures, pytest-html, `--co`/`--setup-*`, empty and deselected runs, lane-count extremes, a lane dying inside the protocol (INTERNALERROR, no hang), `KeyboardInterrupt`, `pytest.exit`, and hybrid restart exhaustion. A 3,000-test × 200-lane stress run on 3.12 and 3.14t produced every report exactly once.

### Silent corruption is made loud

A race that crashes is found. The dangerous kind would leave a run green while a report, an output or a fixture value belonged to another test. Two defences:

1. **A run-time integrity check in every run (`integrity.py`).** On each lane, every routed `logstart`/`logreport`/`logfinish` must name the item that lane is running. On the main thread, each lane's replayed stream must nest: logstart, that item's reports, logfinish. When a lane reports an item done, the replay must just have shown its logstart and logfinish: this is exact, because a lane's hook calls precede its `ItemDone` in one FIFO queue and the lane waits for its acknowledgement. In single-process mode, a run that was not stopped must have run every collected item as many times as it was collected. Any violation fails the run with INTERNALERROR (exit 3), listing what was wrong. It uses only lanes' own state: no new touchpoint.
   - A plugin that logs a report for an item other than the one running would be flagged. No known plugin does (rerunfailures and subtests report the running item).
   - **xdist hides worker INTERNALERRORs:** its controller prints one and the run can still exit 0 (xdist 3.8.0, plain `-n`: "3 passed", exit 0). The hybrid controller (`LanesController`) sets the exit status to INTERNALERROR whenever a worker reports one, so a failed check in a hybrid worker cannot look green.
2. **A canary suite under maximum switching pressure (`tests/test_integrity.py`).** 200 tests on 48 lanes (or 2 × 24), with `sys.setswitchinterval(1e-6)`. Each test checks that its session, module, class and function fixtures, `worker_id`, `tmp_path` and caplog records are its own, and prints, logs and writes to stderr a token 30 times. Every section of every report must contain only its test's token, exactly 30 times, and the report-log rows must equal plain xdist's. Repeat it with `RUNS=N scripts/matrix.sh` to hunt rare races.

Still out of reach of both: process-global state mutated by the tests themselves (F1), and output from threads a test spawns (F3). For F1 there is a third tool, below.

### Finding shared state before it bites: `--lanes-detect`

The integrity check proves the plugin's own bookkeeping; it cannot see a test using another test's global. `--lanes-detect` finds that state ahead of time, in a sequential run (it refuses `--lanes` and `-n`, because concurrent tests would make every snapshot ambiguous).

- **Snapshots.** Before setup (A), after the call phase with fixtures still active (B), and after teardown (C). They cover process state (environment, cwd, `sys.path`, logging levels, signal handlers) and a recursive walk from the globals of every module under the rootdir (plus `lanes_detect_modules`), the class attributes of classes they define, and every registered plugin object except pytest's, pluggy's, xdist's and lanes' own (lanes already isolate those). Objects are expanded when their type comes from one of those packages, so a plugin holding other plugins, lists, dicts or nested objects is followed. Each object is expanded once per snapshot, which bounds the walk and makes cycles safe.
- **Read-only.** Attributes come from `__dict__` and `__slots__`, so no property, `__getattr__` or `__eq__` runs; containers are read through the base `dict`/`list` methods; strings are kept as length and hash only.
- **Live recording** (touchpoint D1, plus public APIs): `mock.patch` and `patch.dict` (so pytest-mock too), `pytest.MonkeyPatch`, and the `os.putenv`/`os.unsetenv`/`os.chdir` audit events. These catch changes made and undone inside one test body.
- **Classification.** Changed and restored within a test, or changed in two or more tests: `per-test` (unsafe). Patched in a test: `patched` (unsafe). A container growing in two or more tests: `grows` (check). Changed in exactly one test, then stable: `set-once` (a cache; fine if thread-safe). Paths inside an already reported path of the same or higher severity are left out.
- **Checked on real code.** Run with pytest-failure-instrumentation active, it reported exactly the per-test state its lane-support design names: the recorder's counters and attempt, its state slot's nodeid and phase, the heartbeat identity, and the stall detector's `activity['main']`.

Limits: state inside C extensions, objects deeper than `lanes_detect_max_depth`, objects of packages not inspected (they are compared by identity only), and external resources (ports, files, database rows), which collide under plain xdist too.

## Sizing: processes × lanes

Measured on a 1-core sandbox, running 1,000 trivial I/O tests (3s each):

| Layout | Wall time | Peak RSS, all processes |
|---|---|---|
| 1 × 1000 | 4.4s | 93 MB |
| 4 × 250 | 7.4s | 244 MB |
| 20 × 50 | 14.5s | 848 MB |

Each process costs one interpreter plus a full collection. That was about 40 MB here; for your suite it's 150–500 MB. The extra wall time is interpreter startup on one core, and on a multi-core machine it would largely overlap.

So choose P for isolation, and use lanes for throughput. Reasons to raise P:

- **Crash blast radius.** One segfault or OOM fails every in-flight test in that process, so up to M−1 innocent tests are reported as crashed. They do rerun on the replacement worker, but their failure reports remain.
- **Hang containment.** Threads can't be killed, but a process can. An external watchdog that kills a wedged worker lets xdist replace it and reschedule the work. This is the practical answer to hung tests.
- **GIL headroom.** If each test's infrastructure spends a fraction c of its time on CPU, one process saturates at roughly 1/c concurrent lanes. For example, 2% CPU per test caps out around 50 lanes per core.
- **Per-lane fixture memory,** which is multiplied by M within each process.

Reasons to lower P: base memory per process, which is P × 150–500 MB for you, and collection time per process.

A reasonable starting point is 8–16 processes × 25–50 lanes. Then adjust using measured CPU and memory per process.

## Plugin compatibility

| Class | Single-process lanes | Hybrid |
|---|---|---|
| A: report consumers (terminal, junitxml, report-log, html) | Work | Work |
| B: xdist observers (`report.node`, node hooks) | `report.node` is the lane; node hooks only via opt-in allowlist | Real xdist node hooks (per process); `report.lane_id` gives the lane |
| C: xdist protocol users (pytest-cov, pytest-metadata) | Run in single-process mode | See a normal xdist run |
| D: execution-side plugins | Run concurrently in threads, so must be thread-safe | Same |

## Flags: no complete fix

| # | Issue |
|---|---|
| F1 | **Process-global state is the biggest risk:** `mock.patch`, `monkeypatch` on shared modules, `os.environ`, `chdir`, signals, `caplog.set_level`. Audit it, and route affected tests with `lanes_exclusive`. |
| F2 | **Warnings:** `catch_warnings` isn't thread-safe before Python 3.14. Use `-X context_aware_warnings=1` on 3.14+ (the default on 3.14t), or `-p no:warnings`, which makes `filterwarnings` marks inert. |
| F3 | **Output from threads your tests spawn** is only attributed with `-X thread_inherit_context=1` on Python 3.14. fd-level writes are never attributed per test. |
| F4 | **Hung tests:** threads can't be killed. In hybrid mode, use a process watchdog. In single-process mode there's no answer yet. |
| F5 | **Crash collateral:** in-flight tests on sibling lanes are reported as crashed. They rerun, but the reports remain. |
| F6 | **Exclusive tests pause lanes.** In hybrid mode an exclusive test waits for, and then blocks, every lane in its process; with hour-long tests that can drain the process for hours. Keep `capsys`/`capfd`/`recwarn` tests out of long suites, or run them in a separate plain invocation. |
| F7 | **`each` and `worksteal` modes are unsupported.** worksteal is implementable. `--pdb` is unsupported, as it is under xdist. |
| F8 | **Ctrl-C** doesn't interrupt running tests. |
| F9 | **`--lanes` means lanes per process in hybrid mode.** The total is `-n` × `--lanes`. |
| F10 | **Wall-clock assertions in `tests/`** are load-sensitive. The round-1 intermittent failure was most likely the P7 basetemp race, now fixed. |
| F11 | **`PYTEST_XDIST_WORKER` is per process.** Fixed for `worker_id`, `testrun_uid`, `xdist.get_xdist_worker_id()` and `config.workerinput`, which name the lane (P10). An environment variable cannot differ per thread, so code that reads `PYTEST_XDIST_WORKER` (or `PYTEST_XDIST_WORKER_COUNT`) sees the process's value. Switch it to `worker_id` or `xdist.get_xdist_worker_id(request)`. |
| F12 | **Warning capture before Python 3.14** swaps process-wide state: 5 of 6 concurrent `pytest.warns` blocks failed. `pytest.warns`, `deprecated_call` and `recwarn` now fail closed in a test that is not `lanes_exclusive` (P11). `warnings.catch_warnings` used directly, in tests or libraries, cannot be guarded. On 3.14+ with context-aware warnings all of these are safe. |
| F13 | **Per-test timeouts are refused (fail closed) until backlog item 3.** In single-process mode pytest-timeout cannot use signals on a lane thread, so it falls back to its thread method, which `os._exit`s the whole process: one timeout would end every lane, with no reports. It is therefore refused there, whether set by option, ini, `PYTEST_TIMEOUT` or marker, and allowed in hybrid mode, where xdist replaces the killed worker. `faulthandler_timeout` is refused in both lane modes, because its process-wide timer never fires for the hung test. Backlog item 3 (`--lanes-timeout`) is the real fix. |
| F14 | **`signal.signal` in a test raises `ValueError`** under lanes: Python allows it on the main thread only. |
| F15 | **Worker-side `pytest_runtest_logreport` consumers see reports late.** In xdist a conftest's implementation also runs synchronously in the worker; under lanes it runs only on the main thread, after the test's teardown. For example, a fixture teardown that reads what a conftest `logreport` recorded finds nothing. Use `pytest_runtest_makereport`, which runs on the lane. |
| F16 | **Per-item overhead on GIL builds.** Each item waits for the main thread to replay its reports. With many CPU-busy lanes the main thread rarely gets the GIL: 3,000 tiny tests took 13s on 1 lane but 108s on 4 lanes and about 200s on 20–200 lanes on 3.12 (7s on 3.14t). It is negligible for long I/O-bound tests. |

## Compatibility plan

- Feature probes, fail-closed.
- A CI matrix: the oldest supported pytest/xdist, the current releases, and pytest plus xdist `main` nightly.
- Upper-bound pins, raised only after the contract suite passes.
- A parity job that runs a real slice of your suite under plain `-n`, under `--lanes`, and under `-n --lanes`, then diffs report-log.
- Upstream candidates: a public per-context SetupState and fixture cache in pytest (removes P1, P2 and P6), a documented node protocol plus a lane-capable worker hook in xdist (removes X1–X4), a `pop(..., None)` fix for `PYTEST_CURRENT_TEST`, and making a worker's INTERNALERROR fail an xdist run (3.8.0 can exit 0).

## Not yet tested

- pytest-cov, Allure, your instrumentation plugin, and your real infrastructure.
- A machine with more than 4 cores.
- A real watchdog.

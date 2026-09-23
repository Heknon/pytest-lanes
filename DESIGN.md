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
- Report hooks are replayed on the main thread with `report.node` set to the lane. This follows xdist's controller/worker hook split exactly.

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
| P7 | `config._tmp_path_factory.getbasetemp` | Serializes pytest's lazy basetemp creation across lanes (see below) |
| X1 | xdist scheduler protocol | Semi-public; probed on each scheduler instance |
| X2 | `WorkerInteractor.channel` / `.sendevent` / `.item_index` | Hybrid mode only |
| X3 | `DSession.handle_crashitem` | Hybrid mode only; reports the 2nd and later crashed lanes of one worker |

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

## Compatibility plan

- Feature probes, fail-closed.
- A CI matrix: the oldest supported pytest/xdist, the current releases, and pytest plus xdist `main` nightly.
- Upper-bound pins, raised only after the contract suite passes.
- A parity job that runs a real slice of your suite under plain `-n`, under `--lanes`, and under `-n --lanes`, then diffs report-log.
- Upstream candidates: a public per-context SetupState and fixture cache in pytest (removes P1, P2 and P6), a documented node protocol plus a lane-capable worker hook in xdist (removes X1–X3), and a `pop(..., None)` fix for `PYTEST_CURRENT_TEST`.

## Not yet tested

- pytest-cov, Allure, your instrumentation plugin, and your real infrastructure.
- A machine with more than 4 cores.
- A real watchdog.

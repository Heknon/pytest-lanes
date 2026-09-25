# Changelog

Versions follow [semantic versioning](https://semver.org). The version lives in
`pyproject.toml` only; a release is a `vX.Y.Z` tag (see README → Releasing).

## 0.1.0

First release.

- Three modes from one xdist scheduler: `-n P` (plain xdist), `--lanes N` (one process,
  N thread lanes) and `-n P --lanes M` (P xdist processes × M lanes).
- A lane behaves like an xdist worker: xdist's own scheduler decides, fixtures of every
  scope are cached per lane, and reporters see xdist's hook split. Report-log output is
  identical in all three modes.
- Fail closed: every pytest, pluggy and xdist internal used is probed at startup.
- Run-time integrity check: a report stream that does not match what the lanes ran fails
  the run (INTERNALERROR), in every mode.
- Per-lane state: output and log capture, `contextlib.redirect_stdout`, basetemp,
  `worker_id`/`workerinput`, `PYTEST_CURRENT_TEST` and `PYTEST_XDIST_WORKER`.
- Patch guard: a process-wide patch (mock, monkeypatch, direct environment writes,
  `os.chdir`) in a test that is not `lanes_exclusive` fails with instructions;
  `--lanes-allow-patches` opts out.
- `--lanes-detect`: a sequential run that reports which tests and fixtures change
  process-wide state.
- Requires Python 3.12 or later. Compatible with pytest 8.0–9.x and pytest-xdist 3.6–3.x;
  tested on CPython 3.12, 3.13, 3.14 and 3.14t with pytest 8.0.2, 8.3.5, 8.4.2 and 9.1.1 and
  pytest-xdist 3.6.1, 3.7.0 and 3.8.0.
- MIT licence.

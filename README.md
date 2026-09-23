# pytest-lanes (prototype)

This plugin runs pytest-xdist's schedulers on thread lanes, either in one process or across many xdist processes. Long, I/O-bound tests can then run thousands-wide without paying 150–500 MB per concurrent test.

```bash
uv pip install -e ".[test]"
pytest -n 8                       # plain xdist (unchanged)
pytest --lanes 200                # 1 process x 200 lanes
pytest -n 8 --lanes 25            # 8 processes x 25 lanes
```

You must also pass one of these:

- On Python 3.13 or earlier: `-p no:warnings`.
- On Python 3.14 or later: `-X context_aware_warnings=1` (already the default on free-threaded 3.14t).

On pytest 8.3.5 and earlier, also pass `-p no:threadexception -p no:unraisableexception`.

Scheduling works exactly as it does in xdist. Use `--dist`/`--lanes-dist` with `load`, `loadscope`, `loadfile` or `loadgroup`, or return your own scheduler from `pytest_xdist_make_scheduler`.

To make a test run alone, use `@pytest.mark.lanes_exclusive`. Tests that use capsys, capfd or recwarn are made exclusive automatically.

Further reading: CLAUDE.md is the maintainer and agent brief, and DESIGN.md has the rationale, evidence and known limitations.

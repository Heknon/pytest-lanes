# pytester runs each scenario in a subprocess (runpytest_subprocess), never in-process:
# lanes patch process-wide state (FixtureDef, sys.stdout, pluggy's hookexec).
pytest_plugins = ["pytester"]

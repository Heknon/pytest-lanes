import logging, time, threading, pytest
SETUPS = []
@pytest.fixture(scope="session")
def env(request):
    SETUPS.append(threading.current_thread().name)
    yield f"env@{threading.current_thread().name}"
@pytest.fixture
def counter():
    return {"n": 0}

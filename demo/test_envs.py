import logging, time, pytest
log = logging.getLogger("demo")
ORDER = {}
def _step(group, i, counter, caplog):
    ORDER.setdefault(group, []).append(i)
    assert ORDER[group] == list(range(i + 1)), ORDER[group]   # in-lane order preserved
    counter["n"] += 1
    assert counter["n"] == 1                                   # function fixture not shared
    print(f"out:{group}:{i}")
    log.warning("log:%s:%s", group, i)
    time.sleep(1)
    assert [r.getMessage() for r in caplog.records] == [f"log:{group}:{i}"]   # caplog isolated

@pytest.mark.parametrize("i", range(3))
@pytest.mark.xdist_group(name="envA")
def test_a(i, counter, caplog, env): _step("A", i, counter, caplog)
@pytest.mark.parametrize("i", range(3))
@pytest.mark.xdist_group(name="envB")
def test_b(i, counter, caplog, env): _step("B", i, counter, caplog)
@pytest.mark.parametrize("i", range(3))
@pytest.mark.xdist_group(name="envC")
def test_c(i, counter, caplog, env): _step("C", i, counter, caplog)
@pytest.mark.xdist_group(name="envD")
def test_d_fails():
    print("visible-only-for-this-test"); time.sleep(1); assert False, "boom"
def test_serial(capsys):
    print("hi"); assert capsys.readouterr().out == "hi\n"

Smoke demo: 4 environment groups, ordering and isolation asserted inside the tests.

    cd demo
    python -m pytest -p no:warnings --lanes 4 --lanes-dist loadgroup -v -rA
    python -m pytest -p no:warnings -n 2 --lanes 2 --dist loadgroup -v -rA

`test_d_fails` fails on purpose, to show that failure output is attributed to the right test.

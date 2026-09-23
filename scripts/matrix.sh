#!/usr/bin/env bash
# Run the contract suite against Python x pytest/xdist versions, each in its own uv venv.
# Usage: scripts/matrix.sh [python ...]      default: 3.12 3.13 3.14 3.14t
# Needs uv and network access to PyPI (or a mirror: set UV_INDEX_URL). Missing interpreters
# are fetched with `uv python install`; use a uv recent enough to know 3.14 final, not an rc.
# RUNS=N repeats the suite N times per combo (flakiness check).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHONS=("$@")
[ ${#PYTHONS[@]} -gt 0 ] || PYTHONS=(3.12 3.13 3.14 3.14t)
RUNS=${RUNS:-1}
COMBOS=(
  "pytest==8.0.2 pytest-xdist==3.6.1 pytest-rerunfailures==14.0"
  "pytest==8.3.5 pytest-xdist==3.6.1 pytest-rerunfailures==14.0"
  "pytest==9.1.1 pytest-xdist==3.8.0 pytest-rerunfailures"
)
rc=0
for py in "${PYTHONS[@]}"; do
  for combo in "${COMBOS[@]}"; do
    name=py$py-$(echo "$combo" | tr ' =' '_-' | cut -c1-60)
    venv="$ROOT/.matrix/$name"
    [ -d "$venv" ] || uv venv -q -p "$py" "$venv"
    uv pip install -q -p "$venv/bin/python" $combo pytest-reportlog pytest-html -e "$ROOT"
    for run in $(seq "$RUNS"); do
      echo "=== $("$venv/bin/python" -VV | head -1) | $combo | run $run/$RUNS"
      (cd "$ROOT" && "$venv/bin/python" -m pytest tests -q -p no:cacheprovider -p no:warnings) || rc=1
    done
  done
done
exit $rc

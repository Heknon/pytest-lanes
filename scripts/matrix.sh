#!/usr/bin/env bash
# Run the contract suite against several pytest/xdist versions, each in its own venv.
# Usage: scripts/matrix.sh [python]      (needs network access to PyPI, or a local mirror)
set -euo pipefail
PY=${1:-python3}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
COMBOS=(
  "pytest==8.0.2 pytest-xdist==3.6.1 pytest-rerunfailures==14.0"
  "pytest==8.3.5 pytest-xdist==3.6.1 pytest-rerunfailures==14.0"
  "pytest==9.1.1 pytest-xdist==3.8.0 pytest-rerunfailures"
)
rc=0
for combo in "${COMBOS[@]}"; do
  name=$(echo "$combo" | tr ' =' '_-' | cut -c1-60)
  venv="$ROOT/.matrix/$name"
  [ -d "$venv" ] || "$PY" -m venv "$venv"
  "$venv/bin/pip" install -q $combo pytest-reportlog pytest-html -e "$ROOT"
  echo "=== $combo"
  (cd "$ROOT" && "$venv/bin/python" -m pytest tests -q -p no:cacheprovider -p no:warnings) || rc=1
done
exit $rc

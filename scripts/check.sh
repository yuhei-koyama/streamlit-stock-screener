#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${project_root}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "Missing .venv. Run: bash scripts/setup.sh" >&2
  exit 1
fi

cd "${project_root}"
"${python_bin}" -m unittest discover -s tests -t . -v
"${python_bin}" -m compileall -q app.py stock_screener tests

echo "Unit tests and Python compilation passed."

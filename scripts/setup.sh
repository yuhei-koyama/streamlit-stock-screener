#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="${VENV_DIR:-${project_root}/.venv}"

# Explicit interpreter wins; preserve an existing environment by default.
if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="${PYTHON_BIN}"
elif [[ -x "${venv_dir}/bin/python" ]]; then
  python_bin="${venv_dir}/bin/python"
elif command -v python3.14 >/dev/null 2>&1; then
  python_bin="python3.14"
elif command -v python3.11 >/dev/null 2>&1; then
  python_bin="python3.11"
else
  python_bin="python3"
fi

python_version="$("${python_bin}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "${python_version}" in
  3.11|3.14) ;;
  *) echo "Python 3.11 or 3.14 is required; found ${python_version}. Set PYTHON_BIN explicitly." >&2; exit 1 ;;
esac

if [[ ! -x "${venv_dir}/bin/python" ]]; then
  "${python_bin}" -m venv "${venv_dir}"
fi
venv_version="$("${venv_dir}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${venv_version}" != "${python_version}" ]]; then
  echo "Existing environment uses Python ${venv_version}, requested ${python_version}. Use a separate VENV_DIR; nothing was deleted." >&2
  exit 1
fi

"${venv_dir}/bin/python" -m pip install --upgrade "pip>=24.3,<27"
"${venv_dir}/bin/python" -m pip install --requirement "${project_root}/requirements-dev.txt"
echo "Environment ready: ${venv_dir} (Python ${venv_version})"

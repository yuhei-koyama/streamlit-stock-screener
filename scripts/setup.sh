#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v python3.11 >/dev/null 2>&1; then
  python_bin="python3.11"
else
  python_bin="python3"
fi

python_version="$(${python_bin} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${python_version}" != "3.11" ]]; then
  echo "Python 3.11 is required; found ${python_version} via ${python_bin}." >&2
  exit 1
fi

venv_dir="${project_root}/.venv"
if [[ ! -x "${venv_dir}/bin/python" ]]; then
  "${python_bin}" -m venv "${venv_dir}"
fi

venv_version="$("${venv_dir}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${venv_version}" != "3.11" ]]; then
  echo "Existing .venv uses Python ${venv_version}; recreate it with Python 3.11." >&2
  exit 1
fi

"${venv_dir}/bin/python" -m pip install --upgrade "pip>=24.3,<27"
"${venv_dir}/bin/python" -m pip install --requirement "${project_root}/requirements-dev.txt"

echo "Environment ready: ${venv_dir} (Python ${venv_version})"

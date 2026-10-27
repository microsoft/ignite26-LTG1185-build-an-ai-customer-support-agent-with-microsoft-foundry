#!/bin/sh
# azd postdeploy hook (macOS/Linux): run scripts/setup.py in a local virtual environment.
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -x "$root/.venv/bin/python" ]; then
  echo "Creating .venv for the setup script..."
  python3 -m venv "$root/.venv"
fi
"$root/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$root/scripts/requirements.txt"
"$root/.venv/bin/python" "$root/scripts/setup.py"
#!/usr/bin/env bash
# CHAPPIE setup script (macOS / Linux)
# Creates a virtual environment in .venv and installs Python dependencies.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Checking for Python 3..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install it from https://www.python.org/downloads/ and re-run."
  exit 1
fi
python3 --version

echo "==> Creating virtual environment in .venv ..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading pip..."
python -m pip install --upgrade pip

echo "==> Installing CHAPPIE dependencies..."
python -m pip install -r backend/requirements.txt

echo "==> Installing Chromium for browser automation (~150 MB)..."
python -m playwright install chromium || \
    echo "    (skipping; you can run 'python -m playwright install chromium' later)"

mkdir -p data logs

echo
echo "All done. Start CHAPPIE with:  ./scripts/start.sh"

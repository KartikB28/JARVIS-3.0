#!/usr/bin/env bash
# Build CHAPPIE as a standalone app (PyInstaller).

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -d ".venv" ]; then
  echo "No .venv found. Run ./scripts/setup.sh first."
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Ensuring build deps..."
python -m pip install --quiet --upgrade pip pyinstaller pywebview

rm -rf build dist

echo "==> Running PyInstaller (this takes 1-3 minutes)..."
python -m PyInstaller --noconfirm --clean chappie.spec

echo
echo "Done. App is at:  dist/CHAPPIE/CHAPPIE"
echo "Make a launcher shortcut, or tar the dist/CHAPPIE folder for distribution."

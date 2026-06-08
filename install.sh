#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== QuotaHalo Unified GNOME Installer ==="
echo ""

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: Python 3 is required."
    echo "Install it first, for example: sudo apt install python3 python3-pip"
    exit 1
fi

echo "[1/2] Installing Python packages..."
python3 -m pip install --user -r "${REPO_DIR}/requirements.txt"

echo "[2/2] Installing GNOME extension and refresh services..."
"${REPO_DIR}/install-gnome-extension.sh"

echo ""
echo "Installed unified Copilot/Codex/Claude usage monitor."

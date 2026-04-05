#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${CCPROXY_INSTALL_ROOT:-$HOME/.local/share/ccproxy}"
BIN_DIR="${CCPROXY_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$INSTALL_ROOT/venv"
PYTHON_BIN="${PYTHON:-python3}"
EDITABLE="${CCPROXY_EDITABLE:-0}"

echo "Installing ccproxy..."
echo "  source: $ROOT_DIR"
echo "  install root: $INSTALL_ROOT"
echo "  bin dir: $BIN_DIR"

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel >/dev/null

if [ "$EDITABLE" = "1" ]; then
  "$VENV_DIR/bin/pip" install -e "$ROOT_DIR"
else
  "$VENV_DIR/bin/pip" install "$ROOT_DIR"
fi

ln -sf "$VENV_DIR/bin/ccproxy" "$BIN_DIR/ccproxy"

echo
echo "Done."
echo "You can now run:"
echo "  ccproxy --help"
echo
echo "If '$BIN_DIR' is not in PATH, add this to your shell profile:"
echo "  export PATH=\"$BIN_DIR:\$PATH\""

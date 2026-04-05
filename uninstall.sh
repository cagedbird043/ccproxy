#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${CCPROXY_INSTALL_ROOT:-$HOME/.local/share/ccproxy}"
BIN_DIR="${CCPROXY_BIN_DIR:-$HOME/.local/bin}"

rm -f "$BIN_DIR/ccproxy"
rm -rf "$INSTALL_ROOT"

echo "Removed ccproxy from:"
echo "  $INSTALL_ROOT"
echo "  $BIN_DIR/ccproxy"

#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${CCPROXY_INSTALL_ROOT:-$HOME/.local/share/ccproxy}"
BIN_DIR="${CCPROXY_BIN_DIR:-$HOME/.local/bin}"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
ZDOTDIR="${ZDOTDIR:-$HOME}"
ZSH_RC="$ZDOTDIR/.zshrc"
BASH_RC="$HOME/.bashrc"
FISH_USER_COMPLETION_DIR="$XDG_CONFIG_HOME/fish/completions"
ZSH_BLOCK_START="# >>> ccproxy completion >>>"
ZSH_BLOCK_END="# <<< ccproxy completion <<<"
BASH_BLOCK_START="# >>> ccproxy completion >>>"
BASH_BLOCK_END="# <<< ccproxy completion <<<"

strip_managed_block() {
  local file="$1"
  local start="$2"
  local end="$3"
  local tmp

  if [ ! -f "$file" ]; then
    return 0
  fi

  tmp="$(mktemp)"
  awk -v start="$start" -v end="$end" '
    $0 == start { skip=1; next }
    $0 == end { skip=0; next }
    !skip { print }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

rm -f "$BIN_DIR/ccproxy"
rm -rf "$INSTALL_ROOT"
rm -f "$FISH_USER_COMPLETION_DIR/ccproxy.fish"
strip_managed_block "$ZSH_RC" "$ZSH_BLOCK_START" "$ZSH_BLOCK_END"
strip_managed_block "$BASH_RC" "$BASH_BLOCK_START" "$BASH_BLOCK_END"

echo "Removed ccproxy from:"
echo "  $INSTALL_ROOT"
echo "  $BIN_DIR/ccproxy"
echo "  $FISH_USER_COMPLETION_DIR/ccproxy.fish"

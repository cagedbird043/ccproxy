#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${CCPROXY_INSTALL_ROOT:-$HOME/.local/share/ccproxy}"
BIN_DIR="${CCPROXY_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$INSTALL_ROOT/venv"
PYTHON_BIN="${PYTHON:-python3}"
EDITABLE="${CCPROXY_EDITABLE:-0}"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
ZDOTDIR="${ZDOTDIR:-$HOME}"
CURRENT_SHELL="${CCPROXY_SHELL:-${SHELL##*/}}"
COMPLETION_ROOT="$INSTALL_ROOT/completions"
ZSH_COMPLETION_DIR="$COMPLETION_ROOT/zsh"
BASH_COMPLETION_DIR="$COMPLETION_ROOT/bash"
FISH_COMPLETION_DIR="$COMPLETION_ROOT/fish"
FISH_USER_COMPLETION_DIR="$XDG_CONFIG_HOME/fish/completions"
ZSH_RC="$ZDOTDIR/.zshrc"
BASH_RC="$HOME/.bashrc"
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

install_managed_block() {
  local file="$1"
  local start="$2"
  local end="$3"
  local body="$4"

  mkdir -p "$(dirname "$file")"
  touch "$file"
  strip_managed_block "$file" "$start" "$end"
  {
    printf '\n%s\n' "$start"
    printf '%s\n' "$body"
    printf '%s\n' "$end"
  } >> "$file"
}

install_completion_files() {
  mkdir -p "$ZSH_COMPLETION_DIR" "$BASH_COMPLETION_DIR" "$FISH_COMPLETION_DIR" "$FISH_USER_COMPLETION_DIR"
  "$VENV_DIR/bin/ccproxy" completion zsh > "$ZSH_COMPLETION_DIR/_ccproxy"
  "$VENV_DIR/bin/ccproxy" completion bash > "$BASH_COMPLETION_DIR/ccproxy.bash"
  "$VENV_DIR/bin/ccproxy" completion fish > "$FISH_COMPLETION_DIR/ccproxy.fish"
  ln -sf "$FISH_COMPLETION_DIR/ccproxy.fish" "$FISH_USER_COMPLETION_DIR/ccproxy.fish"
}

install_zsh_activation() {
  local body
  body="$(cat <<EOF
if [ -d "$ZSH_COMPLETION_DIR" ]; then
  fpath=("$ZSH_COMPLETION_DIR" \$fpath)
  autoload -Uz _ccproxy 2>/dev/null || true
  if whence -w compdef >/dev/null 2>&1; then
    compdef _ccproxy ccproxy
  else
    autoload -Uz compinit
    compinit
    compdef _ccproxy ccproxy
  fi
fi
EOF
)"
  install_managed_block "$ZSH_RC" "$ZSH_BLOCK_START" "$ZSH_BLOCK_END" "$body"
}

install_bash_activation() {
  local body
  body="$(cat <<EOF
if [ -f "$BASH_COMPLETION_DIR/ccproxy.bash" ]; then
  . "$BASH_COMPLETION_DIR/ccproxy.bash"
fi
EOF
)"
  install_managed_block "$BASH_RC" "$BASH_BLOCK_START" "$BASH_BLOCK_END" "$body"
}

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
install_completion_files

case "$CURRENT_SHELL" in
  zsh)
    install_zsh_activation
    ;;
  bash)
    install_bash_activation
    ;;
  fish)
    ;;
esac

echo
echo "Done."
echo "You can now run:"
echo "  ccproxy --help"
echo "  ccproxy completion zsh"
echo
echo "If '$BIN_DIR' is not in PATH, add this to your shell profile:"
echo "  export PATH=\"$BIN_DIR:\$PATH\""
echo
echo "Shell completion:"
echo "  generated for: bash, zsh, fish"
echo "  current shell wiring: $CURRENT_SHELL"
if [ "$CURRENT_SHELL" = "zsh" ]; then
  echo "  zsh rc updated: $ZSH_RC"
elif [ "$CURRENT_SHELL" = "bash" ]; then
  echo "  bash rc updated: $BASH_RC"
elif [ "$CURRENT_SHELL" = "fish" ]; then
  echo "  fish completion linked: $FISH_USER_COMPLETION_DIR/ccproxy.fish"
fi

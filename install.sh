#!/usr/bin/env bash
# session-channel installer.
#
# Defaults:
#   - Install root:   $SESSION_CHANNEL_HOME (or $HOME/.session-channel)
#   - CLI symlink:    $HOME/.local/bin/channel
#   - Python:         python3 on PATH (override with $SESSION_CHANNEL_PY)
#
# Idempotent: re-running upgrades deps in place.

set -euo pipefail

SESSION_CHANNEL_HOME="${SESSION_CHANNEL_HOME:-$HOME/.session-channel}"
PY="${SESSION_CHANNEL_PY:-python3}"
BIN_DIR="${SESSION_CHANNEL_BIN:-$HOME/.local/bin}"

# Allow this script to be run either from inside a checkout
# (./install.sh) or piped (curl … | bash). When run from a
# checkout, copy in place; otherwise expect SESSION_CHANNEL_HOME to
# already contain the source.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$HERE" != "$SESSION_CHANNEL_HOME" ]]; then
  echo "==> Staging source from $HERE → $SESSION_CHANNEL_HOME"
  mkdir -p "$SESSION_CHANNEL_HOME"
  # Copy everything except install artifacts.
  rsync -a --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.venv' \
    --exclude 'node_modules' \
    "$HERE/" "$SESSION_CHANNEL_HOME/"
fi

cd "$SESSION_CHANNEL_HOME"

echo "==> Creating venv at $SESSION_CHANNEL_HOME/.venv"
if command -v uv >/dev/null 2>&1; then
  uv venv .venv --python "$PY"
  # shellcheck disable=SC1091
  source .venv/bin/activate
  uv pip install -e .
else
  "$PY" -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -e .
fi

echo "==> Symlinking CLI → $BIN_DIR/channel"
mkdir -p "$BIN_DIR"
ln -sf "$SESSION_CHANNEL_HOME/cli/channel.py" "$BIN_DIR/channel"
chmod +x "$SESSION_CHANNEL_HOME/cli/channel.py"

echo
echo "Installed. Add $BIN_DIR to PATH if not already:"
echo "  export PATH=\"$BIN_DIR:\$PATH\""
echo
echo "Smoke test:"
echo "  channel topics      # connects to Redis at \$redis_url"
echo
echo "Start the service:"
echo "  cd $SESSION_CHANNEL_HOME && .venv/bin/uvicorn main:app --host 127.0.0.1 --port 10101"

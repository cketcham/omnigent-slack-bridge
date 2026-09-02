#!/usr/bin/env bash
# Install the Omnigent Slack bridge to ~/.local/bin and the systemd user unit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$SCRIPT_DIR/omnigent_slack_bridge.py"
BIN_DIR="$HOME/.local/bin"
SYSTEMD_DIR="$HOME/.config/systemd/user"

mkdir -p "$BIN_DIR" "$SYSTEMD_DIR"

install -m 0755 "$SRC" "$BIN_DIR/omnigent-slack-bridge"
echo "installed binary -> $BIN_DIR/omnigent-slack-bridge"

install -m 0644 "$SCRIPT_DIR/systemd/omnigent-slack-bridge.service" "$SYSTEMD_DIR/omnigent-slack-bridge.service"
echo "installed unit   -> $SYSTEMD_DIR/omnigent-slack-bridge.service"
echo
echo "next:"
echo "  mkdir -p ~/.config/omnigent-slack-bridge"
echo "  # write ~/.config/omnigent-slack-bridge/config.env (see README)"
echo "  omnigent-slack-bridge auth        # verify tokens"
echo "  omnigent-slack-bridge poll        # run in foreground, or:"
echo "  systemctl --user daemon-reload && systemctl --user enable --now omnigent-slack-bridge"

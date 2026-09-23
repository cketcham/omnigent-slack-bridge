# Ensure the Omnigent daemons (token-keeper + slack-bridge) are running.
# Idempotent: checks for the tmux sessions and starts them if missing.
# This survives workspace restarts (which kill tmux): the next shell that
# opens (any SSH login) brings both daemons back.

# Skip if tmux is unavailable.
command -v tmux >/dev/null 2>&1 || return 0

_omni_bridgedir="$HOME/omnigent-slack-bridge"
[[ -d "$_omni_bridgedir" && -x "$_omni_bridgedir/run.sh" ]] || { unset _omni_bridgedir; return 0 }

# Token keeper: refreshes the Omnigent JWT before it expires (~8h lifetime).
if [[ -f "$HOME/omnigent-token-keeper.py" ]]; then
  if ! tmux has-session -t omnigent-token-keeper 2>/dev/null; then
    tmux new-session -d -s omnigent-token-keeper -c "$HOME" \
      "bash $HOME/omnigent-slack-bridge/run-keeper.sh 2>&1 | tee -a $HOME/.local/share/omnigent-slack-bridge/keeper.log"
  fi
fi

# Slack bridge: needs the workspace Slack secrets to be mounted.
if [[ -f "/run/user/$(id -u)/secrets/SLACK_BOT_TOKEN" ]]; then
  if ! tmux has-session -t omnigent-slack-bridge 2>/dev/null; then
    tmux new-session -d -s omnigent-slack-bridge -c "$HOME" \
      "bash $_omni_bridgedir/run.sh 2>&1 | tee -a $HOME/.local/share/omnigent-slack-bridge/poll.log"
  fi
fi
unset _omni_bridgedir

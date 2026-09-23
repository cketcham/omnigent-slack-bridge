#!/usr/bin/env bash
# Run the omnigent token-keeper with auto-restart on crash.
# Deploys to the workspace as ~/omnigent-slack-bridge/run-keeper.sh.
while true; do
  python3 -u ~/omnigent-token-keeper.py "$@"
  code=$?
  echo "[run-keeper.sh] keeper exited ($code); restarting in 5s..." >&2
  sleep 5
done

# omnigent-slack-bridge

A standalone daemon that gives each of your **Omnigent** sessions its own Slack
channel — a bidirectional bridge so you can follow and reply to your agents
from your phone.

- When a session is **waiting** (blocked on you) it pings you (once per channel).
- When a turn ends (**idle**) it mirrors the agent's new assistant output into the
  channel so you can see the reply without opening Omnigent.
- You reply **top-level** in the channel (no threads) and the text is forwarded
  into the session as a user message via `POST /v1/sessions/{id}/events`.

This is the successor to
[`herdr-slack-bridge`](https://github.com/cketcham/herdr-slack-bridge)
(now deprecated), and it's **simpler**: no plugin hooks, no socket paths, no
tmux, no transcript-file parsing. One daemon talks to the Omnigent HTTP API
(Bearer JWT) and the Slack Web API (bot token).

```
   Omnigent server                         Slack
        │                                    │
        │  WS /v1/sessions/updates (stream)  │  per-session channel
        │  GET /v1/sessions/{id}/items       │  #ck-<project>-<title>
        │  ──────────────────────────────▶   │  (renamed live as title changes)
        │     (blocked alert / mirrored      │
        │      assistant reply)              │
        │                                    │  you reply (top-level)
        │  POST /v1/sessions/{id}/events     │  ◀── Socket Mode (real-time push)
        │  ◀──────────────────────────────   │
        │     (forwarded as a user message)  │
```

Channel names: `#<prefix>-<project>-<title-slug>` when the session is in a
project, or `#<prefix>-<title-slug>` when it isn't — e.g.
`#ck-git-parity-rejection-reply` or `#ck-omni-slack-bridge`. The channel
name tracks the session title live (renamed via `conversations.rename` when
the title changes), so the channel stays recognizable as Omnigent auto-renames
the session. If the name is already taken by another session, the session
shortid is appended. Routing is by channel id stored in local state (keyed by
session id).

## Why this is easier than the Herdr version

| Herdr bridge | Omnigent bridge |
|---|---|
| One-shot plugin event hooks + a separate poller daemon, juggling `HERDR_SESSION`/`HERDR_SOCKET_PATH` | **One daemon, fully event-driven.** Streams session updates via `WS /v1/sessions/updates`; receives Slack replies via Socket Mode. |
| Read pi's JSONL transcript file directly — agent-specific, only pi worked | `GET /v1/sessions/{id}/items` — structured, **harness-agnostic** (pi, claude, codex, …) |
| `herdr pane run` = typing into a tmux pane | A proper user-message event — no tty contention |
| Routing by opaque pane-id | Sessions carry `agent_name`, `title`, `workspace` directly |

## Status mapping

Omnigent session `status` is `idle` / `running` / `waiting` / `failed`.

- `waiting` (or `pending_elicitations_count > 0`) → **blocked** alert (you're @mentioned once).
- `idle` (transition into idle) → mirror the agent's new assistant output.
- `failed` → a one-shot alert.
- `running` → nothing to say.

## Prerequisites

- The `omnigent` CLI installed and **logged in** (`omnigent login`) — the bridge
  reads the server URL from `~/.omnigent/config.yaml` and the bearer JWT from
  `~/.omnigent/auth_tokens.json`. The token is an ~8h JWT; the bridge re-reads
  it on 401, but it relies on your normal `omnigent` CLI use (or a periodic
  `omnigent login`) to keep it fresh.
- Python 3.10+ with `slack_sdk` and `websockets` installed (`pip install
  slack_sdk websockets`). Both are pure-Python and lightweight.
- A Slack app with **Socket Mode** and **Event Subscriptions** enabled — see
  **[SETUP.md](SETUP.md)** for the full, copy-pasteable guide (manifest, scopes,
  tokens, event subscriptions, troubleshooting).

## Setup

### 1. Create the Slack app

See **[SETUP.md](SETUP.md)** for the full guide (manifest, Socket Mode, event
subscriptions, tokens, troubleshooting). Short version:

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Paste [`slack-manifest.json`](slack-manifest.json).
3. **Install App** → copy the **Bot User OAuth Token** (`xoxb-...`) and the
   **App-Level Token** (`xapp-...`, from Socket Mode).
4. Find your **Slack user id** (`U...`).

### 2. Write config

```bash
mkdir -p ~/.config/omnigent-slack-bridge
cat > ~/.config/omnigent-slack-bridge/config.env <<'EOF'
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_USER_ID=U...
OMNIGENT_SLACK_BRIDGE_PREFIX=ck
# optional:
# OMNIGENT_SLACK_BRIDGE_PRIVATE=true
# OMNIGENT_SLACK_BRIDGE_ALLOWED_USERS=U0123,U0456
# OMNIGENT_SLACK_BRIDGE_PROJECT=<one Omnigent project name>
# OMNIGENT_SERVER_URL=https://...   # overrides ~/.omnigent/config.yaml
# OMNIGENT_AUTH_TOKEN=<jwt>          # overrides ~/.omnigent/auth_tokens.json
EOF
chmod 600 ~/.config/omnigent-slack-bridge/config.env
```

### 3. Install + run

```bash
# Install Python dependencies
pip install slack_sdk websockets

# Install the binary + systemd unit
scripts/install.sh

omnigent-slack-bridge auth      # confirms Slack + Omnigent tokens
omnigent-slack-bridge scopes    # probes which Slack scopes the token has
omnigent-slack-bridge status    # prints config + tracked sessions
```

Run the daemon (foreground for testing):

```bash
omnigent-slack-bridge poll
```

Or as a systemd user service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now omnigent-slack-bridge.service
```

In a container or anywhere systemd isn't available:

```bash
tmux new-session -d -s omnigent-slack-bridge -c ~ \
  'omnigent-slack-bridge poll 2>&1 | tee -a ~/.local/share/omnigent-slack-bridge/poll.log'
```

### 4. Verify

Start an agent (`omnigent run …`). When it goes `waiting` you'll get a ping in a
new `#ck-<project>-<title>` channel (or `#ck-<title>` if the session has no
project). Reply there (top-level) and the text becomes a user message in the
session. When the turn ends, the agent's reply is mirrored back into the same
channel.

## Session lifecycle (archive / delete / unarchive)

The bridge keeps the Slack channel in sync with the Omnigent session's
lifecycle:

| Omnigent action | Slack channel action |
|---|---|
| Archive session | Archives the channel |
| Unarchive session | Unarchives the channel |
| Delete session | Archives the channel |

Slack does not allow bot tokens to delete channels (`admin.conversations.delete`
requires Enterprise Grid + an admin user token), so a deleted Omnigent session
archives the Slack channel rather than deleting it. The channel is hidden from
the sidebar but preserved — it can be unarchived if the session ever returns.

## Channel naming

| Session has a project? | Channel name |
|---|---|
| Yes | `#<prefix>-<project>-<title>` e.g. `#ck-git-parity-bot-handling` |
| No | `#<prefix>-<title>` e.g. `#ck-omni-slack-bridge` |
| Name conflict | append the session shortid e.g. `#ck-omni-slack-bridge-09c9b1` |

The channel name tracks the session title live — when Omnigent auto-renames a
session, the bridge renames the Slack channel to match (and keeps the topic
synced too).

## Agent renames

If a session's `agent_name` changes (the agent was re-registered under a new
name), the channel name would be stale. The bridge detects the change and
renames the channel live via `conversations.rename`. The channel **topic** is
kept synced to the session `title`.

## Config reference

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | Bot OAuth token (`xoxb-...`). **Required.** |
| `SLACK_APP_TOKEN` | — | App-Level token for Socket Mode (`xapp-...`). **Required** for real-time inbound. |
| `OMNIGENT_SLACK_BRIDGE_PREFIX` | `ck` | Channel-name prefix. **Required.** |
| `SLACK_USER_ID` | — | Your Slack user id; `@mentioned` once on the first blocked. |
| `OMNIGENT_SERVER_URL` | from `~/.omnigent/config.yaml` | Omnigent server. |
| `OMNIGENT_AUTH_TOKEN` | from `~/.omnigent/auth_tokens.json` | Bearer JWT. |
| `OMNIGENT_SLACK_BRIDGE_PRIVATE` | `false` | Create private channels. |
| `OMNIGENT_SLACK_BRIDGE_ALLOWED_USERS` | (any) | Comma list of Slack user ids allowed to reply. |
| `OMNIGENT_SLACK_BRIDGE_PROJECT` | (none) | Scope to one Omnigent project; empty = all your sessions. |
| `OMNIGENT_SLACK_BRIDGE_STATE_DIR` | `~/.local/share/omnigent-slack-bridge` | State file location. |

## Commands

| Command | What it does |
|---|---|
| `poll` | Run the bridge daemon (outbound + inbound). |
| `status` | Print config + tracked sessions. |
| `auth` | Probe Slack `auth.test` + Omnigent `list_sessions`. |
| `scopes` | Probe which Slack scopes the token has. |
| `verify` | Show recent messages in each tracked channel. |
| `find` | List channels matching the prefix. |
| `archive <id>` / `unarchive <id>` | Archive / unarchive a channel by id. |
| `config-dir` / `state-dir` | Print the config / state directory. |

## Notes and limitations

- The bridge forwards **any** top-level message you send in an agent channel to
  that session, regardless of the session's current state. If the agent is
  mid-turn, the message is still delivered as a user turn.
- Channels are created lazily on the first `waiting`/`idle`/`failed` event — a
  session that's only ever `running` (no output yet) gets no channel.
- On first channel creation, `last_mirror_id` is seeded to the newest item id,
  so only turns that happen **after** the channel exists are mirrored (no
  one-time history dump).
- State is local per machine (`~/.local/share/omnigent-slack-bridge/state.json`).
  Each machine's bridge only creates/reads its own channels, so replies route to
  the machine that owns the channel.
- The Omnigent JWT expires (~8h). If you see `auth_expired (run \`omnigent
  login\`)` in the logs, run `omnigent login` to refresh it.

## Uninstall

```bash
systemctl --user disable --now omnigent-slack-bridge.service 2>/dev/null
tmux kill-session -t omnigent-slack-bridge 2>/dev/null
rm -f ~/.local/bin/omnigent-slack-bridge \
      ~/.config/systemd/user/omnigent-slack-bridge.service
rm -rf ~/.config/omnigent-slack-bridge ~/.local/share/omnigent-slack-bridge
```

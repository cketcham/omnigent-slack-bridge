# omnigent-slack-bridge

A standalone daemon that gives each of your **Omnigent** sessions its own Slack
channel — a bidirectional bridge so you can follow and reply to your agents
from your phone.

- When a session is **waiting** (blocked on you) it pings you (once per channel).
- When a turn ends (**idle**) it mirrors the agent's new assistant output into the
  channel so you can see the reply without opening Omnigent.
- You reply **top-level** in the channel (no threads) and the text is forwarded
  into the session as a user message via `POST /v1/sessions/{id}/events`.

This is the Omnigent counterpart to
[`herdr-slack-bridge`](../herdr-slack-bridge), and it's **simpler**: no plugin
hooks, no socket paths, no tmux, no transcript-file parsing. One daemon talks
to the Omnigent HTTP API (Bearer JWT) and the Slack Web API (bot token).

```
   Omnigent server                         Slack
        │                                    │
        │  GET /v1/sessions (poll)           │  per-session channel
        │  GET /v1/sessions/{id}/items       │  #ck-<agent>
        │  ──────────────────────────────▶   │  (#ck-<agent>-<shortid> on conflict)
        │     (blocked alert / mirrored      │
        │      assistant reply)              │
        │                                    │  you reply (top-level)
        │  POST /v1/sessions/{id}/events     │  ◀── conversations.history poll
        │  ◀──────────────────────────────   │
        │     (forwarded as a user message)  │
```

Channel names: `#<prefix>-<agent_name>` e.g. `#ck-pi-native-ui`. If that name
is already taken (another session runs the same agent), the bridge appends the
session shortid — `#ck-pi-native-ui-b8f1a7` — so each session still gets its
own channel. Routing is by channel id stored in local state (keyed by session id).

## Why this is easier than the Herdr version

| Herdr bridge | Omnigent bridge |
|---|---|
| One-shot plugin event hooks + a separate poller daemon, juggling `HERDR_SESSION`/`HERDR_SOCKET_PATH` | **One daemon.** Polls `GET /v1/sessions`; sends replies via `POST /v1/sessions/{id}/events`. |
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
- Python 3.10+ (stdlib only — nothing to `pip install`; `pyyaml` is used if
  available to parse `config.yaml`, with a regex fallback).
- A Slack workspace where you can create an app.

## Setup

### 1. Create the Slack app

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Paste [`slack-manifest.json`](slack-manifest.json).
3. **Install App** to your workspace.
4. **OAuth & Permissions** → copy the **Bot User OAuth Token** (`xoxb-...`).
5. Find your **Slack user id** (`U...`): Slack → Profile → ⋯ → Copy member ID.

The manifest requests: `chat:write`, `channels:manage`, `channels:history`,
`channels:read`, `invites:write` (plus `groups:*` for private channels).

### 2. Write config

```bash
mkdir -p ~/.config/omnigent-slack-bridge
cat > ~/.config/omnigent-slack-bridge/config.env <<'EOF'
SLACK_BOT_TOKEN=xoxb-...
SLACK_USER_ID=U...
OMNIGENT_SLACK_BRIDGE_PREFIX=ck
# optional:
# OMNIGENT_SLACK_BRIDGE_PRIVATE=true
# OMNIGENT_SLACK_BRIDGE_POLL_INTERVAL=5
# OMNIGENT_SLACK_BRIDGE_ALLOWED_USERS=U0123,U0456
# OMNIGENT_SLACK_BRIDGE_PROJECT=<one Omnigent project name>
# OMNIGENT_SERVER_URL=https://...   # overrides ~/.omnigent/config.yaml
# OMNIGENT_AUTH_TOKEN=<jwt>          # overrides ~/.omnigent/auth_tokens.json
EOF
chmod 600 ~/.config/omnigent-slack-bridge/config.env
```

### 3. Install + run

```bash
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
new `#ck-<agent>-<shortid>` channel. Reply there (top-level) and the text
becomes a user message in the session. When the turn ends, the agent's reply is
mirrored back into the same channel.

## Agent renames

If a session's `agent_name` changes (the agent was re-registered under a new
name), the channel name would be stale. The bridge detects the change,
archives the old channel, and creates a fresh one — the same proven approach
the Herdr bridge uses (Slack's `conversations.rename` is admin-gated and
flaky, so archive + recreate is more reliable). The channel **topic** is kept
synced to the session `title`, so you can recognize a channel even as Omnigent
auto-renames the session.

## Config reference

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | Bot OAuth token (`xoxb-...`). Required. |
| `OMNIGENT_SLACK_BRIDGE_PREFIX` | `ck` | Channel-name prefix. Required. |
| `SLACK_USER_ID` | — | Your Slack user id; `@mentioned` once on the first blocked. |
| `OMNIGENT_SERVER_URL` | from `~/.omnigent/config.yaml` | Omnigent server. |
| `OMNIGENT_AUTH_TOKEN` | from `~/.omnigent/auth_tokens.json` | Bearer JWT. |
| `OMNIGENT_SLACK_BRIDGE_POLL_INTERVAL` | `5` | Seconds between ticks. |
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

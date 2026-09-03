# Setup Guide

This guide walks you through creating your own Slack app and configuring the
Omnigent Slack Bridge from scratch. The bridge needs a Slack app with Socket
Mode enabled (for real-time inbound messages) and event subscriptions (so the
app actually receives message events).

## 1. Create the Slack app

1. Go to **https://api.slack.com/apps** and click **Create New App**.
2. Choose **From an app manifest**.
3. Pick your workspace.
4. Paste the contents of [`slack-manifest.json`](slack-manifest.json) (in this
   repo). The manifest pre-configures the app name, bot scopes, Socket Mode,
   and event subscriptions in one step.
5. Click **Create**.

> **If you already have an app** (e.g. you created one before the manifest
> included event subscriptions), see [Manual setup](#manual-setup-no-manifest)
> below instead — you can update your existing app in place.

## 2. Enable Socket Mode

Socket Mode lets the bridge receive messages via a persistent websocket instead
of polling `conversations.history` (which is rate-limited). The manifest
enables this automatically, but if you're configuring manually:

1. In your app's left sidebar, click **Socket Mode** (under "Settings").
2. Toggle it **ON**.
3. Give the token a name (e.g. "Omnigent Bridge") and click **Generate**.
4. Copy the **App-Level Token** (`xapp-...`). This is your `SLACK_APP_TOKEN`.

## 3. Subscribe to bot events

The app must subscribe to message events so Socket Mode actually receives them.
The manifest does this, but to verify or configure manually:

1. In the left sidebar, click **Event Subscriptions** (under "Features").
2. Toggle it **ON**.
3. Under **Subscribe to bot events**, ensure these are added:
   - `message.channels` — messages in public channels
   - `message.groups` — messages in private channels (if using private channels)
   - `message.im` — direct messages (optional, for DM-based replies)
4. Click **Save Changes**.

## 4. Install the app to your workspace

1. In the left sidebar, click **Install App** (under "Settings").
2. Click **Install to Workspace** and approve the permissions.
3. After install, go to **OAuth & Permissions** and copy the **Bot User OAuth
   Token** (`xoxb-...`). This is your `SLACK_BOT_TOKEN`.

> **Enterprise workspaces:** installing or reinstalling the app (e.g. after
> adding event subscriptions) may require **admin approval**. The install
> request is sent to your workspace admins — you may need to wait for them to
> approve it, or ask in your internal admin channel. Until approved, the app
> won't receive events.

## 5. Find your Slack user id

You'll be `@mentioned` once per channel when an agent is blocked, so the bridge
needs your Slack user id:

1. In Slack, click your profile picture → **Profile**.
2. Click the **⋯** (three dots) menu → **Copy member ID**.
3. The id looks like `U0B4NDR0B5Z`. This is your `SLACK_USER_ID`.

## 6. Write the config

```bash
mkdir -p ~/.config/omnigent-slack-bridge
cat > ~/.config/omnigent-slack-bridge/config.env <<'EOF'
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_USER_ID=U...
OMNIGENT_SLACK_BRIDGE_PREFIX=ck
EOF
chmod 600 ~/.config/omnigent-slack-bridge/config.env
```

The Omnigent server URL and auth token are auto-read from `~/.omnigent/` — no
need to set them unless you want to override:

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | Bot OAuth token (`xoxb-...`). **Required.** |
| `SLACK_APP_TOKEN` | — | App-Level token for Socket Mode (`xapp-...`). **Required** for real-time inbound. |
| `SLACK_USER_ID` | — | Your Slack user id; `@mentioned` once on first blocked. |
| `OMNIGENT_SLACK_BRIDGE_PREFIX` | `ck` | Channel-name prefix. |
| `OMNIGENT_SERVER_URL` | from `~/.omnigent/config.yaml` | Omnigent server URL. |
| `OMNIGENT_AUTH_TOKEN` | from `~/.omnigent/auth_tokens.json` | Bearer JWT. |
| `OMNIGENT_SLACK_BRIDGE_PRIVATE` | `false` | Create private channels. |
| `OMNIGENT_SLACK_BRIDGE_POLL_INTERVAL` | `5` | Seconds between outbound ticks. |
| `OMNIGENT_SLACK_BRIDGE_ALLOWED_USERS` | (any) | Comma list of Slack user ids allowed to reply. |
| `OMNIGENT_SLACK_BRIDGE_PROJECT` | (none) | Scope to one Omnigent project; empty = all your sessions. |
| `OMNIGENT_SLACK_BRIDGE_STATE_DIR` | `~/.local/share/omnigent-slack-bridge` | State file location. |

## 7. Install and run

First, install the Python dependencies (the bridge uses `slack_sdk` for Socket
Mode and `websockets` for the Omnigent session stream):

```bash
pip install slack_sdk websockets
```

Then install the bridge:

```bash
# Install the binary + systemd unit
scripts/install.sh

# Verify both tokens work
omnigent-slack-bridge auth

# Run in the foreground (for testing)
omnigent-slack-bridge poll

# Or as a systemd service
systemctl --user daemon-reload
systemctl --user enable --now omnigent-slack-bridge.service
```

In a container or anywhere systemd isn't available:

```bash
tmux new-session -d -s omnigent-slack-bridge -c ~ \
  'omnigent-slack-bridge poll 2>&1 | tee -a ~/.local/share/omnigent-slack-bridge/poll.log'
```

## 8. Verify

Start an Omnigent agent session. When it goes `waiting` (blocked), you'll get a
ping in a new `#<prefix>-<project>-<title>` channel. Reply there (top-level, no
thread) and the text becomes a user message in the session. When the turn ends,
the agent's reply is mirrored back into the same channel.

```bash
omnigent-slack-bridge status    # prints config + tracked sessions
omnigent-slack-bridge verify    # shows recent messages in each channel
```

---

## Manual setup (no manifest)

If you can't use the manifest (e.g. you're updating an existing app), configure
each section by hand:

### OAuth scopes

Go to **OAuth & Permissions** → **Bot Token Scopes** and add:

- `chat:write` — post messages
- `channels:manage` — create/rename/archive channels
- `channels:history` — read channel history (fallback if no Socket Mode)
- `channels:read` — list channels
- `invites:write` — invite users to channels
- `groups:manage` — create/rename private channels
- `groups:history` — read private channel history
- `groups:read` — list private channels

### Socket Mode

Go to **Socket Mode** → toggle **ON** → generate an app-level token
(`xapp-...`).

### Event Subscriptions

Go to **Event Subscriptions** → toggle **ON** → add `message.channels`,
`message.groups`, `message.im` under **Subscribe to bot events** → **Save
Changes**.

### Reinstall

After changing scopes or event subscriptions, Slack requires you to reinstall
the app. Click **Install App** → **Install to Workspace**. In enterprise
workspaces this may need admin approval.

---

## Troubleshooting

### No inbound messages come through

The most common cause: **event subscriptions aren't enabled** or the app wasn't
reinstalled after enabling them. Check:

1. **Event Subscriptions** is toggled ON.
2. `message.channels` is in the subscribed events list.
3. You **reinstalled** the app after adding events (Slack prompts for this).
4. In enterprise workspaces, the reinstall was **approved** by an admin.

### `ratelimited` errors in the log

This means the bridge is polling `conversations.history` instead of using
Socket Mode. Ensure `SLACK_APP_TOKEN` is set and Socket Mode is enabled in the
app. The bridge logs `inbound=socket-mode` on startup if it's connected.

### `auth_expired (run \`omnigent login\`)`

The Omnigent JWT expired (~8h lifetime). Run `omnigent login` on the machine
running the bridge to refresh it. The bridge re-reads the token from
`~/.omnigent/auth_tokens.json` automatically.

### Channels aren't being created

Check that the bot was invited to the workspace and has `channels:manage`
scope. Run `omnigent-slack-bridge auth` to verify the bot token is valid, and
`omnigent-slack-bridge scopes` to probe which scopes it actually has.

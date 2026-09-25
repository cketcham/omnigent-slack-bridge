#!/usr/bin/env python3
"""omnigent-token-keeper — keep the Omnigent auth token fresh on a workspace.

A tiny daemon that re-logins to the Omnigent server before the session JWT
expires, so the token in ``~/.omnigent/auth_tokens.json`` is always valid for
the CLI, the Slack bridge, and anything else that reads it.

Reads the server URL from ``~/.omnigent/config.yaml`` and the username +
password from ``~/.omnigent/login-credentials``. Re-logins when <1h of token
lifetime remains. Checks every 5 minutes.

Pure stdlib (urllib + json), so there's nothing to pip install.

Usage:
  omnigent-token-keeper          Run the keeper loop (foreground).
  omnigent-token-keeper once     Refresh once if needed, then exit.
  omnigent-token-keeper status    Print token expiry and refresh status.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHECK_INTERVAL = 300  # 5 minutes
REFRESH_THRESHOLD = 3600  # refresh when <1h of lifetime remains
DEFAULT_EXPIRES_IN = 8 * 3600  # 8 hours (server default)


def _config_path() -> Path:
    return Path.home() / ".omnigent" / "config.yaml"


def _tokens_path() -> Path:
    return Path.home() / ".omnigent" / "auth_tokens.json"


def _credentials_path() -> Path:
    return Path.home() / ".omnigent" / "login-credentials"


def _read_server_url() -> str:
    """Read the server URL from ~/.omnigent/config.yaml."""
    path = _config_path()
    if not path.exists():
        return ""
    try:
        text = path.read_text()
        m = re.search(r"^server:\s*(\S+)", text, re.MULTILINE)
        return m.group(1) if m else ""
    except OSError:
        return ""


def _read_credentials() -> tuple[str, str] | None:
    """Read username + password from ~/.omnigent/login-credentials."""
    path = _credentials_path()
    if not path.exists():
        return None
    try:
        lines = path.read_text().strip().splitlines()
        if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
            return lines[0].strip(), lines[1].strip()
    except OSError:
        pass
    return None


def _load_token_entry(server_url: str) -> dict | None:
    path = _tokens_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        entry = data.get(server_url)
        return entry if isinstance(entry, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _store_token(server_url: str, token: str, refresh_token: str, expires_in: int) -> None:
    path = _tokens_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    entry = data.get(server_url) or {}
    entry["token"] = token
    if refresh_token:
        entry["refresh_token"] = refresh_token
    entry["expires_at"] = time.time() + expires_in
    data[server_url] = entry
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2))
    os.replace(tmp, str(path))


def _login(server_url: str, username: str, password: str) -> dict | None:
    """POST /auth/login with issue_refresh=true. Returns the response body or None."""
    body = json.dumps({
        "username": username,
        "password": password,
        "issue_refresh": True,
    }).encode()
    req = urllib.request.Request(
        server_url.rstrip("/") + "/auth/login",
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None


def _probe_token(server_url: str, token: str) -> bool | None:
    """Cheap liveness probe: GET /v1/sessions?limit=1 with the current token.
    True = valid, False = rejected (401 — invalidated by a server redeploy
    even though expires_at claims it's valid), None = unreachable/5xx."""
    req = urllib.request.Request(server_url.rstrip("/") + "/v1/sessions?limit=1")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False
        return None  # 5xx / other — server trouble, not a token problem
    except (urllib.error.URLError, OSError):
        return None


def refresh_if_needed(server_url: str) -> bool:
    """Re-login when the token expires within REFRESH_THRESHOLD **or** when
    a liveness probe shows the server rejects it (a redeploy invalidates
    issued JWTs while their expires_at still claims validity)."""
    entry = _load_token_entry(server_url)
    if entry is None:
        log(f"no token entry for {server_url}")
        return False

    expires_at = entry.get("expires_at", 0)
    if not isinstance(expires_at, (int, float)):
        return False

    remaining = expires_at - time.time()
    reason = None
    if remaining > REFRESH_THRESHOLD:
        # Plenty of lifetime per the clock — but the server may have been
        # redeployed and invalidated the JWT. Probe; only a definitive 401
        # triggers a re-login (5xx/unreachable means server trouble, and
        # logging in then would fail anyway).
        token = entry.get("token")
        if not isinstance(token, str) or not token:
            return False
        probe = _probe_token(server_url, token)
        if probe is not False:
            return False  # healthy (or server unreachable — nothing to do)
        reason = "server rejects token (401) despite valid expiry — redeployed?"
    else:
        reason = f"token expires in {remaining/60:.0f}m"

    creds = _read_credentials()
    if not creds:
        log(f"{reason} but no login-credentials file")
        return False

    username, password = creds
    log(f"{reason}; re-logging in as {username}...")
    result = _login(server_url, username, password)
    if result is None or not result.get("token"):
        log("re-login failed")
        return False

    new_token = result["token"]
    new_refresh = result.get("refresh_token") or ""
    expires_in = result.get("expires_in", DEFAULT_EXPIRES_IN)
    _store_token(server_url, new_token, new_refresh, expires_in)
    user_id = result.get("user", {}).get("id", "?")
    log(f"re-logged in as {user_id}; token valid {expires_in // 3600}h")
    return True


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def run_once() -> int:
    server = _read_server_url()
    if not server:
        log("no server URL in ~/.omnigent/config.yaml")
        return 1
    refresh_if_needed(server)
    return 0


def run_loop() -> int:
    server = _read_server_url()
    if not server:
        log("no server URL in ~/.omnigent/config.yaml")
        return 1
    creds = _read_credentials()
    if not creds:
        log("no ~/.omnigent/login-credentials — cannot auto-refresh")
        return 1
    log(f"omnigent-token-keeper started; server={server} check={CHECK_INTERVAL}s threshold={REFRESH_THRESHOLD}s")
    while True:
        try:
            refresh_if_needed(server)
        except Exception as e:
            log(f"error: {e}")
        time.sleep(CHECK_INTERVAL)


def run_status() -> int:
    server = _read_server_url()
    if not server:
        print("no server URL configured")
        return 1
    entry = _load_token_entry(server)
    if entry is None:
        print(f"server: {server}")
        print("token: (none stored)")
        return 0
    expires_at = entry.get("expires_at", 0)
    remaining = expires_at - time.time()
    has_refresh = bool(entry.get("refresh_token"))
    has_creds = _read_credentials() is not None
    print(f"server:        {server}")
    print(f"expires_at:    {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(expires_at))}")
    print(f"remaining:     {remaining/3600:.1f}h" if remaining > 0 else "expired")
    print(f"refresh_token: {'yes' if has_refresh else 'no'}")
    print(f"credentials:   {'yes' if has_creds else 'no'}")
    print(f"will refresh:   {'yes' if remaining < REFRESH_THRESHOLD and has_creds else 'no'}")
    return 0


USAGE = """omnigent-token-keeper — keep the Omnigent auth token fresh.

Usage:
  omnigent-token-keeper          Run the keeper loop (foreground).
  omnigent-token-keeper once     Refresh once if needed, then exit.
  omnigent-token-keeper status   Print token expiry and refresh status.

Reads ~/.omnigent/config.yaml (server URL) and ~/.omnigent/login-credentials
(username + password). Re-logins when <1h of token lifetime remains.
"""


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "loop"
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if cmd == "once":
        return run_once()
    if cmd == "status":
        return run_status()
    if cmd == "loop":
        return run_loop()
    print(f"unknown command: {cmd}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

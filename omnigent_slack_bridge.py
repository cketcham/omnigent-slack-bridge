#!/usr/bin/env python3
"""omnigent-slack-bridge — per-agent Slack channels for Omnigent sessions.

A standalone daemon that watches your Omnigent sessions and gives each one a
Slack channel. When a session is *waiting* (blocked on you) it pings you; when
a turn ends (*idle*) it mirrors the agent's new assistant output into the
channel so you can see the reply from your phone. You reply top-level in the
channel and the text is forwarded into the session as a user message via
``POST /v1/sessions/{id}/events``.

Channel names: ``#<prefix>-<project>-<title-slug>`` e.g. ``#ck-git-parity-rejection-reply``.
The channel name tracks the session title live (renamed when the title changes),
so the channel stays recognizable as Omnigent auto-renames the session.

Uses ``slack_sdk`` for Slack Socket Mode (real-time inbound) and
``websockets`` for the Omnigent session-update stream (real-time outbound).
Talks to the Omnigent HTTP API (Bearer JWT from
``~/.omnigent/auth_tokens.json``) and the Slack Web API (bot token). No
plugin hooks, no socket paths, no transcript-file parsing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_PREFIX = "ck"


def _default_config_dir() -> str:
    return str(Path.home() / ".config" / "omnigent-slack-bridge")


def _default_state_dir() -> str:
    return str(Path.home() / ".local" / "share" / "omnigent-slack-bridge")


def _default_server_url() -> str:
    """Read the configured Omnigent server from ~/.omnigent/config.yaml."""
    path = Path.home() / ".omnigent" / "config.yaml"
    if not path.exists():
        return ""
    try:
        import yaml  # part of the omnigent install; optional
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("server") or ""
    except Exception:
        # Fall back to a naive regex if pyyaml isn't importable.
        try:
            text = path.read_text()
            m = re.search(r"^server:\s*(\S+)", text, re.MULTILINE)
            return m.group(1) if m else ""
        except Exception:
            return ""


def _load_token(server_url: str) -> str:
    """Read the bearer JWT for ``server_url`` from ~/.omnigent/auth_tokens.json."""
    entry = _load_token_entry(server_url) or {}
    return entry.get("token") or ""


def _load_token_entry(server_url: str) -> dict[str, Any] | None:
    path = Path.home() / ".omnigent" / "auth_tokens.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get(server_url) or {}
    except Exception:
        return None


def _store_token(server_url: str, token: str, refresh_token: str, prev: dict[str, Any] | None, expires_in: int = 8 * 3600) -> None:
    """Persist a refreshed access token (and new refresh token) back to disk."""
    path = Path.home() / ".omnigent" / "auth_tokens.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        data = {}
    entry = data.get(server_url) or {}
    if prev:
        entry.update(prev)
    entry["token"] = token
    if refresh_token:
        entry["refresh_token"] = refresh_token
    entry["expires_at"] = time.time() + expires_in
    data[server_url] = entry
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2))
    os.replace(tmp, str(path))


def _load_login_credentials() -> tuple[str, str] | None:
    """Read username + password from ~/.omnigent/login-credentials.
    Returns (username, password) or None if the file is missing/malformed."""
    path = Path.home() / ".omnigent" / "login-credentials"
    try:
        lines = path.read_text().strip().splitlines()
        if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
            return lines[0].strip(), lines[1].strip()
    except Exception:
        pass
    return None


@dataclass
class Config:
    slack_bot_token: str = ""
    slack_app_token: str = ""  # xapp-... for Socket Mode (real-time inbound)
    slack_user_id: str = ""
    prefix: str = DEFAULT_PREFIX
    private: bool = False
    server_url: str = ""
    auth_token: str = ""
    allowed_users: list[str] = field(default_factory=list)
    state_dir: str = _default_state_dir()
    project: str = ""  # optional Omnigent project filter

    def validate(self) -> list[str]:
        missing = []
        if not self.slack_bot_token:
            missing.append("SLACK_BOT_TOKEN")
        if not self.slack_app_token:
            missing.append("SLACK_APP_TOKEN (Socket Mode — see SETUP.md)")
        if not self.prefix:
            missing.append("OMNIGENT_SLACK_BRIDGE_PREFIX")
        if not self.server_url:
            missing.append("OMNIGENT_SERVER_URL (or ~/.omnigent/config.yaml server)")
        return missing


def _env_file_paths() -> list[str]:
    paths = []
    if os.environ.get("OMNIGENT_SLACK_BRIDGE_CONFIG_DIR"):
        paths.append(os.path.join(os.environ["OMNIGENT_SLACK_BRIDGE_CONFIG_DIR"], "config.env"))
    paths.append(os.path.join(_default_config_dir(), "config.env"))
    return paths


def _load_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("\"'")
    return out


def load_config() -> Config:
    merged: dict[str, str] = {}
    for p in _env_file_paths():
        merged.update(_load_env_file(p))
    merged.update({k: v for k, v in os.environ.items()})

    def get(k: str) -> str:
        return merged.get(k, "")

    server_url = get("OMNIGENT_SERVER_URL") or _default_server_url()
    auth_token = get("OMNIGENT_AUTH_TOKEN") or _load_token(server_url)

    c = Config(
        slack_bot_token=get("SLACK_BOT_TOKEN"),
        slack_app_token=get("SLACK_APP_TOKEN"),
        slack_user_id=get("SLACK_USER_ID"),
        prefix=get("OMNIGENT_SLACK_BRIDGE_PREFIX"),
        private=_bool(get("OMNIGENT_SLACK_BRIDGE_PRIVATE")),
        server_url=server_url,
        auth_token=auth_token,
        allowed_users=_list(get("OMNIGENT_SLACK_BRIDGE_ALLOWED_USERS")),
        state_dir=get("OMNIGENT_SLACK_BRIDGE_STATE_DIR") or _default_state_dir(),
        project=get("OMNIGENT_SLACK_BRIDGE_PROJECT"),
    )
    return c


def _bool(v: str) -> bool:
    return v.lower() in ("1", "true", "yes", "on")


def _list(v: str) -> list[str]:
    return [p.strip() for p in v.split(",") if p.strip()]


def _mask(t: str) -> str:
    if not t:
        return "(unset)"
    if len(t) <= 10:
        return "***"
    return t[:6] + "..." + t[-4:]


# ──────────────────────────────────────────────────────────────────────────
# HTTP helpers (stdlib only)
# ──────────────────────────────────────────────────────────────────────────

class ApiError(Exception):
    def __init__(self, source: str, err: str, payload: Any = None):
        super().__init__(f"{source}: {err}")
        self.source = source
        self.err = err
        self.payload = payload


def _http_request(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return e.code, e.read(), hdrs
    except urllib.error.URLError as e:
        raise ApiError("http", str(e))


# ──────────────────────────────────────────────────────────────────────────
# Omnigent client
# ──────────────────────────────────────────────────────────────────────────

class OmnigentClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.server_url.rstrip("/")
        self._token = cfg.auth_token

    @property
    def token(self) -> str:
        return self._token

    def _reload_token(self) -> bool:
        """Get a fresh token: try the server's refresh endpoint first (using
        the refresh_token stored alongside the JWT), then fall back to
        re-reading the JWT from disk (the CLI may have refreshed it)."""
        if self._refresh_via_server():
            return True
        t = _load_token(self.cfg.server_url)
        if t and t != self._token:
            self._token = t
            return True
        return False

    def _refresh_via_server(self) -> bool:
        """Try to get a fresh token. The token-keeper daemon is the primary
        mechanism; this is a fallback for when the keeper isn't running.
        Tries refresh_token exchange, then re-login with stored credentials."""
        # Strategy 1: refresh token exchange.
        entry = _load_token_entry(self.cfg.server_url) or {}
        rt = entry.get("refresh_token") if entry else ""
        if rt:
            body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rt}).encode()
            url = self.base + "/oauth/token"
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status != 200:
                        return False
                    out = json.loads(resp.read().decode())
            except Exception:
                return False
            new_token = out.get("access_token") or out.get("token") or ""
            new_refresh = out.get("refresh_token") or ""
            if not new_token:
                return False
            self._token = new_token
            _store_token(self.cfg.server_url, new_token, new_refresh, entry)
            return True
        # Strategy 2: re-login with stored credentials (fallback if keeper isn't running).
        creds = _load_login_credentials()
        if not creds:
            return False
        username, password = creds
        body = json.dumps({"username": username, "password": password, "issue_refresh": True}).encode()
        url = self.base + "/auth/login"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    return False
                out = json.loads(resp.read().decode())
        except Exception:
            return False
        new_token = out.get("token") or ""
        new_refresh = out.get("refresh_token") or ""
        if not new_token:
            return False
        self._token = new_token
        expires_in = out.get("expires_in", 8 * 3600)
        _store_token(self.cfg.server_url, new_token, new_refresh, entry, expires_in)
        log(f"auth: re-logged in as {out.get('user', {}).get('id', '?')}; token valid {expires_in // 3600}h")
        return True

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, query: dict[str, str] | None = None, body: Any = None) -> Any:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(2):
            status, raw, _ = _http_request(method, url, self._headers(), data)
            if status == 401 and attempt == 0 and self._reload_token():
                continue  # retry once with a refreshed token
            if status == 401:
                raise ApiError("omnigent", "auth_expired (run `omnigent login`)", raw.decode(errors="replace"))
            if 200 <= status < 300:
                if not raw:
                    return None
                return json.loads(raw.decode())
            raise ApiError("omnigent", f"http {status}", raw.decode(errors="replace"))
        raise ApiError("omnigent", "unreachable")

    def list_my_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Top-level sessions I can see (kind=default excludes sub-agents)."""
        params = {"limit": str(limit), "kind": "default", "order": "desc", "sort_by": "updated_at"}
        if self.cfg.project:
            params["project"] = self.cfg.project
        res = self._call("GET", "/v1/sessions", params)
        return res.get("data", []) if res else []

    def list_items(self, session_id: str, limit: int = 200, order: str = "asc", after: str = "") -> list[dict[str, Any]]:
        params = {"limit": str(limit), "order": order}
        if after:
            params["after"] = after
        res = self._call("GET", f"/v1/sessions/{session_id}/items", params)
        return res.get("data", []) if res else []

    def send_message(self, session_id: str, text: str) -> None:
        self._call("POST", f"/v1/sessions/{session_id}/events", body={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        })

    def list_projects(self) -> dict[str, str]:
        """Return {project_id: project_name} for the caller's projects."""
        res = self._call("GET", "/v1/projects")
        return {p["id"]: p.get("name", "") for p in (res.get("data") or [])}


# ──────────────────────────────────────────────────────────────────────────
# Slack client (Web API, stdlib only)
# ──────────────────────────────────────────────────────────────────────────

SLACK_API = "https://slack.com/api"


class SlackClient:
    # Global rate-limit backoff: when Slack returns `ratelimited`, all calls
    # pause until this monotonic deadline. Shared across all SlackClient
    # instances in the process (one bridge = one client, so effectively global).
    _rate_limit_until: float = 0.0

    def __init__(self, token: str):
        self.token = token

    def _call(self, method: str, params: dict[str, str], post: bool = True) -> dict[str, Any]:
        # Honor a prior ratelimited response before even trying.
        wait = SlackClient._rate_limit_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        url = SLACK_API + "/" + method
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if post:
            data = urllib.parse.urlencode(params).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            url += "?" + urllib.parse.urlencode(params)
        status, raw, resp_headers = _http_request("POST" if post else "GET", url, headers, data)
        try:
            out = json.loads(raw.decode())
        except Exception:
            raise ApiError(f"slack.{method}", f"http {status} (non-json)")
        if not out.get("ok"):
            # On ratelimited, read Retry-After (seconds) and set the global
            # backoff so the next call waits. Slack sends it as a header and
            # also in the JSON body for socket-mode-style calls.
            if out.get("error") == "ratelimited":
                # Slack sends Retry-After as an HTTP header (seconds).
                # Fall back to the JSON body field (socket-mode) or 30s default.
                ra = resp_headers.get("retry-after") or out.get("retry_after") or "30"
                try:
                    retry_after = float(ra)
                except (ValueError, TypeError):
                    retry_after = 30
                SlackClient._rate_limit_until = time.monotonic() + retry_after
                log(f"slack {method} ratelimited; backing off {retry_after}s")
            raise ApiError(f"slack.{method}", out.get("error", "unknown"), out)
        return out

    def create_channel(self, name: str, private: bool) -> str:
        params = {"name": name}
        if private:
            params["is_private"] = "true"
        res = self._call("conversations.create", params)
        ch = res.get("channel") or {}
        cid = ch.get("id") or ""
        if not cid:
            raise ApiError("slack.create", "no channel id")
        return cid

    def invite_user(self, channel_id: str, user_id: str) -> None:
        if not user_id:
            return
        try:
            self._call("conversations.invite", {"channel": channel_id, "users": user_id})
        except ApiError as e:
            if e.err not in ("already_in_channel", "is_archived"):
                raise

    def post_message(self, channel_id: str, text: str) -> str:
        try:
            res = self._call("chat.postMessage", {"channel": channel_id, "text": text})
            return res.get("ts") or ""
        except ApiError as e:
            if e.err in ("not_in_channel", "channel_not_found"):
                return ""
            raise

    def archive(self, channel_id: str) -> None:
        try:
            self._call("conversations.archive", {"channel": channel_id})
        except ApiError as e:
            if e.err not in ("already_archived", "not_in_channel", "channel_not_found"):
                raise

    def unarchive(self, channel_id: str) -> None:
        try:
            self._call("conversations.unarchive", {"channel": channel_id})
        except ApiError as e:
            if e.err not in ("not_archived", "not_in_channel", "channel_not_found"):
                raise

    def set_topic(self, channel_id: str, topic: str) -> None:
        try:
            self._call("conversations.setTopic", {"channel": channel_id, "topic": topic[:1024]})
        except ApiError:
            pass  # best-effort; not_in_channel, scope loss, etc.

    def rename(self, channel_id: str, name: str) -> bool:
        """Rename a channel. Returns True on success. Best-effort: a failure
        (e.g. scope loss, not in channel) is logged but never fatal."""
        try:
            self._call("conversations.rename", {"channel": channel_id, "name": name})
            return True
        except ApiError as e:
            if e.err in ("not_in_channel", "channel_not_found"):
                log(f"slack.rename {channel_id}: channel gone, marking closed")
                return False
            log(f"slack.rename {channel_id}: {e.err}")
            return False

    def lookup_by_name(self, name: str, private: bool) -> str:
        types = "private_channel" if private else "public_channel"
        cursor = ""
        for _ in range(20):
            res = self._call("conversations.list", {"limit": "200", "types": types, "cursor": cursor}, post=False)
            for ch in res.get("channels") or []:
                if (ch.get("name") or "").lower() == name.lower():
                    return ch.get("id") or ""
            cursor = (res.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return ""


# ──────────────────────────────────────────────────────────────────────────
# Channel naming
# ──────────────────────────────────────────────────────────────────────────

_SAFE = re.compile(r"[^a-z0-9_-]+")


def _safe_part(s: str) -> str:
    s = (s or "").lower().strip()
    s = _SAFE.sub("-", s)
    return re.sub(r"-+", "-", s).strip("-_")


def _slug(s: str, max_len: int = 40) -> str:
    """Slugify a string for use in a channel name, truncated to max_len."""
    return _safe_part(s)[:max_len].rstrip("-_")


def channel_name(prefix: str, project: str, title: str) -> str:
    """Preferred channel name: #<prefix>-<project>-<title-slug>.
    Falls back to #<prefix>-<title> when there's no project, then to
    #<prefix>-<project> when there's no title. The shortid is appended only
    on a name conflict (see conflict_name)."""
    p = _slug(project, 30) if project else ""
    t = _slug(title, 40) if title else ""
    parts = [prefix, p, t]
    name = "-".join(x for x in parts if x)
    name = _SAFE.sub("-", name)
    name = re.sub(r"-+", "-", name).strip("-_")
    if len(name) > 80:
        name = name[:80].rstrip("-_")
    return name or "omnigent-agent"


def conflict_name(prefix: str, project: str, title: str, session_id: str) -> str:
    """Fallback when the preferred name is taken: append the session shortid."""
    short = (session_id or "")[-6:]
    base = channel_name(prefix, project, title)
    return f"{base}-{short}"[:80].rstrip("-_")


# ──────────────────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class SessionRecord:
    session_id: str
    project: str = ""
    title: str = ""
    channel_id: str = ""
    channel_name: str = ""
    last_status: str = ""
    last_mirror_id: str = ""
    last_seen_ts: str = ""
    mentioned: bool = False
    closed: bool = False
    created_at: int = 0


class StateStore:
    def __init__(self, dirpath: str):
        self.path = os.path.join(dirpath, "state.json")
        os.makedirs(dirpath, exist_ok=True)
        import threading
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(Path(self.path).read_text())
        except OSError:
            return {"sessions": {}}
        except json.JSONDecodeError:
            return {"sessions": {}}

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        Path(tmp).write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)

    def update(self, fn) -> None:
        import threading
        with self._lock:
            data = self._load()
            sessions: dict[str, Any] = data.setdefault("sessions", {})
            fn(sessions)
            self._save(data)

    def records(self) -> list[SessionRecord]:
        data = self._load()
        out = []
        for sid, r in (data.get("sessions") or {}).items():
            out.append(SessionRecord(
                session_id=sid,
                project=r.get("project", ""),
                title=r.get("title", ""),
                channel_id=r.get("channel_id", ""),
                channel_name=r.get("channel_name", ""),
                last_status=r.get("last_status", ""),
                last_mirror_id=r.get("last_mirror_id", ""),
                last_seen_ts=r.get("last_seen_ts", ""),
                mentioned=r.get("mentioned", False),
                closed=r.get("closed", False),
                created_at=r.get("created_at", 0),
            ))
        return out

    def get(self, sessions: dict[str, Any], sid: str) -> SessionRecord:
        r = sessions.setdefault(sid, {"session_id": sid})
        return SessionRecord(
            session_id=sid,
            project=r.get("project", ""),
            title=r.get("title", ""),
            channel_id=r.get("channel_id", ""),
            channel_name=r.get("channel_name", ""),
            last_status=r.get("last_status", ""),
            last_mirror_id=r.get("last_mirror_id", ""),
            last_seen_ts=r.get("last_seen_ts", ""),
            mentioned=r.get("mentioned", False),
            closed=r.get("closed", False),
            created_at=r.get("created_at", 0),
        )


# ──────────────────────────────────────────────────────────────────────────
# Bridge
# ──────────────────────────────────────────────────────────────────────────

class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.omni = OmnigentClient(cfg)
        self.slack = SlackClient(cfg.slack_bot_token)
        self.store = StateStore(cfg.state_dir)
        self._projects: dict[str, str] | None = None  # project_id -> name cache
        self._socket_mode = False
        self._seen_inbound_ts: set[str] = set()  # dedup inbound Slack messages

    def _project_name(self, s: dict[str, Any]) -> str:
        """Resolve a session's project to a human name. Returns empty string
        when the session has no project — the channel name then uses just
        prefix + title (e.g. #ck-omni-slack-bridge)."""
        pid = s.get("project_id") or ""
        if pid:
            if self._projects is None:
                try:
                    self._projects = self.omni.list_projects()
                except ApiError:
                    self._projects = {}
            return self._projects.get(pid, "")
        return ""

    def _handle_session(self, s: dict[str, Any]) -> None:
        sid = s.get("id") or ""
        if not sid:
            return
        project = self._project_name(s)
        title = s.get("title") or ""
        status = s.get("status") or ""
        archived = bool(s.get("archived"))
        pending = int(s.get("pending_elicitations_count") or 0)
        blocked = status == "waiting" or pending > 0

        def write(sessions: dict[str, Any]) -> None:
            self._mutate(sessions, sid, project, title, status, archived, blocked)

        self.store.update(write)

    def _handle_session_removed(self, sid: str) -> None:
        """A session was deleted from Omnigent. Archive its Slack channel
        (Slack doesn't allow bot tokens to delete channels, only archive)."""
        def write(sessions: dict[str, Any]) -> None:
            rec = self.store.get(sessions, sid)
            if rec.channel_id and not rec.closed:
                self.slack.post_message(rec.channel_id, "📦 session deleted; archiving this channel.")
                self.slack.archive(rec.channel_id)
                sessions[sid]["closed"] = True
                log(f"outbound: session {sid[:12]} deleted; archived #{rec.channel_name}")
        self.store.update(write)

    def _retry_archive(self, sid: str) -> None:
        """Retry archiving a channel for a session that's marked closed in
        state but whose Slack channel is still open (a previous archive
        call failed, e.g. during a rate-limit crash loop)."""
        def write(sessions: dict[str, Any]) -> None:
            rec = self.store.get(sessions, sid)
            if rec.channel_id:
                self.slack.archive(rec.channel_id)
                log(f"outbound: retried archive for {sid[:12]} (#{rec.channel_name})")
        self.store.update(write)

    def _mutate(self, sessions, sid, project, title, status, archived, blocked) -> None:
        rec = self.store.get(sessions, sid)
        if rec.created_at == 0:
            sessions[sid]["created_at"] = int(time.time())

        # Archived session: archive its channel. Unarchived: unarchive it.
        if archived:
            if rec.channel_id and not rec.closed:
                self.slack.post_message(rec.channel_id, "📦 session archived; archiving this channel.")
                self.slack.archive(rec.channel_id)
                sessions[sid]["closed"] = True
            sessions[sid]["last_status"] = status
            return
        else:
            # Session unarchived: unarchive the channel if it was closed.
            if rec.channel_id and rec.closed:
                self.slack.unarchive(rec.channel_id)
                sessions[sid]["closed"] = False
                self.slack.post_message(rec.channel_id, "📦 session unarchived; channel reopened.")

        # If the channel is closed but the session is still active (not archived),
        # the channel was lost (archived externally, bot removed, etc.). Reset
        # channel_id so a fresh channel is created below.
        if rec.closed and not archived:
            sessions[sid]["channel_id"] = ""
            rec = self.store.get(sessions, sid)
            log(f"outbound: channel lost for active session {sid[:12]}; will recreate")

        # The channel name tracks the current project + title, so a title
        # change renames the channel live (Slack supports conversations.rename).
        # A project change (rare) also renames. This keeps the channel name
        # recognizable as Omnigent auto-renames the session.
        want_name = channel_name(self.cfg.prefix, project, title)

        # Log project changes for visibility; the rename happens below.
        if rec.channel_id and rec.project and rec.project != project:
            log(f"project changed: {rec.project} -> {project}; renaming channel")

        sessions[sid]["project"] = project
        sessions[sid]["title"] = title

        # Rename the channel when the desired name diverges from the current one.
        if rec.channel_id and rec.channel_name and rec.channel_name != want_name:
            if self.slack.rename(rec.channel_id, want_name):
                sessions[sid]["channel_name"] = want_name
                rec = self.store.get(sessions, sid)
            else:
                # Rename failed (channel gone/archived) — reset channel_id so
                # a fresh channel is created below.
                sessions[sid]["channel_id"] = ""
                sessions[sid]["closed"] = True
                log(f"outbound: channel gone for {sid[:12]}; will recreate")
                rec = self.store.get(sessions, sid)

        # Keep the channel topic synced to the session title too.
        if rec.channel_id and title and rec.title != title:
            self.slack.set_topic(rec.channel_id, title)

        is_turn_end = status == "idle"
        is_alert = blocked or status == "failed"
        if not (is_alert or is_turn_end):
            sessions[sid]["last_status"] = status
            return  # running / unknown — nothing to say

        # Lazily create the channel on the first actionable event, OR recreate
        # if the channel was lost (archived externally / bot removed).
        if not rec.channel_id or rec.closed:
            cid, cname = self._create_channel(want_name, project, title, sid)
            sessions[sid]["channel_id"] = cid
            sessions[sid]["channel_name"] = cname
            sessions[sid]["closed"] = False
            sessions[sid]["mentioned"] = False
            self.slack.invite_user(cid, self.cfg.slack_user_id)
            label = title or project or "session"
            intro = f"🚀 Channel for *{label}* (`{sid[:8]}`)."
            if project:
                intro += f"\nProject: *{project}*"
            self.slack.post_message(cid, intro)
            self.slack.set_topic(cid, title or project)
            # Seed last_mirror_id to the newest item so we only mirror turns
            # that happen AFTER the channel exists (no history dump).
            try:
                latest = self.omni.list_items(sid, limit=1, order="desc")
                if latest:
                    sessions[sid]["last_mirror_id"] = latest[0].get("id", "")
            except ApiError as e:
                log(f"seed last_mirror_id {sid}: {e}")

        cid = sessions[sid]["channel_id"]

        # Alert on blocked/failed (mention once per channel).
        if is_alert:
            mention = blocked and not rec.mentioned
            text = self._alert_text(title, project, sid, status, mention)
            if mention:
                sessions[sid]["mentioned"] = True
            ts = self.slack.post_message(cid, text)
            if _ts_greater(ts, rec.last_seen_ts):
                sessions[sid]["last_seen_ts"] = ts
            sessions[sid]["last_status"] = status
            return

        # Turn-end: mirror new assistant output.
        if is_turn_end and rec.last_status != "idle":
            new_text, new_last_id = self._new_assistant_text(sid, rec.last_mirror_id)
            if new_text.strip():
                mts = self.slack.post_message(cid, new_text)
                sessions[sid]["last_mirror_id"] = new_last_id
                if _ts_greater(mts, rec.last_seen_ts):
                    sessions[sid]["last_seen_ts"] = mts
            else:
                sessions[sid]["last_mirror_id"] = new_last_id or rec.last_mirror_id
        sessions[sid]["last_status"] = status

    def _create_channel(self, name: str, project: str, title: str, sid: str) -> tuple[str, str]:
        """Create the channel, falling back to a shortid-suffixed name on a
        conflict. Returns (channel_id, actual_name_used)."""
        try:
            return self.slack.create_channel(name, self.cfg.private), name
        except ApiError as e:
            if e.err == "name_taken":
                # Try to find and unarchive the existing channel.
                try:
                    cid = self.slack.lookup_by_name(name, self.cfg.private)
                    if cid:
                        self.slack.unarchive(cid)
                        return cid, name
                except ApiError:
                    pass  # lookup failed (rate limit, etc.) — fall through
                # Name taken and lookup failed: use the shortid-suffixed name.
                fb = conflict_name(self.cfg.prefix, project, title, sid)
                return self.slack.create_channel(fb, self.cfg.private), fb
            raise

    def _alert_text(self, title, project, sid, status, mention) -> str:
        label = title or project or "session"
        if status == "failed":
            head = f"🔴 *{label}* *failed* (`{sid[:8]}`)"
        else:
            head = f"🟡 *{label}* is *blocked* — needs you (`{sid[:8]}`)"
            if mention:
                head += f"  <@{self.cfg.slack_user_id}>"
        return head

    def _new_assistant_text(self, sid: str, last_id: str) -> tuple[str, str]:
        """Return (concatenated new assistant text, new last item id).

        Uses the server-side ``after`` cursor (order=asc) so every returned
        item is strictly newer than ``last_id`` — no fragile id comparison.
        """
        texts: list[str] = []
        new_last = last_id
        cursor = last_id
        for _ in range(5):  # bounded pagination across a long turn
            try:
                items = self.omni.list_items(sid, limit=200, order="asc", after=cursor)
            except ApiError as e:
                log(f"mirror: list items {sid}: {e}")
                return "\n\n".join(texts), new_last
            if not items:
                break
            for it in items:
                if it.get("type") == "message" and it.get("role") == "assistant":
                    for block in it.get("content") or []:
                        if block.get("type") in ("output_text", "text"):
                            t = (block.get("text") or "").strip()
                            if t:
                                texts.append(t)
                new_last = it.get("id", "") or new_last
            cursor = new_last
            if len(items) < 200:
                break
        return "\n\n".join(texts), new_last

    # -- inbound: Socket Mode (real-time push, no polling) -------------

    def _start_socket_mode(self) -> None:
        """Start a Slack Socket Mode client that receives message events in
        real-time via websocket. Replaces conversations.history polling —
        no rate limit, no polling interval."""
        try:
            from slack_sdk.socket_mode.builtin import SocketModeClient
            from slack_sdk.socket_mode.request import SocketModeRequest
            from slack_sdk.web import WebClient
        except ImportError:
            log("inbound: slack_sdk not installed — install with: pip install slack_sdk")
            self._socket_mode = False
            return

        app_token = self.cfg.slack_app_token
        if not app_token:
            log("inbound: no SLACK_APP_TOKEN — see SETUP.md")
            self._socket_mode = False
            return

        self._socket_mode = True
        client = SocketModeClient(app_token=app_token)
        client.web_client = WebClient(token=self.cfg.slack_bot_token)

        def handler(cli, req: SocketModeRequest) -> None:
            if req.type == "events_api":
                event = req.payload.get("event", {})
                if event.get("type") == "message" and not event.get("bot_id") and not event.get("subtype"):
                    self._handle_inbound_message(event)
            # Acknowledge the request so Slack doesn't retry.
            cli.send_socket_mode_response(req.to_response())

        client.socket_mode_request_listeners.append(handler)
        client.connect()
        log("inbound: socket mode connected (real-time message push)")

    def _handle_inbound_message(self, event: dict[str, Any]) -> None:
        """Forward a Slack user message to the matching Omnigent session.
        Deduplicates by message ts — Slack may retry Socket Mode events."""
        channel_id = event.get("channel") or ""
        user = event.get("user") or ""
        text = (event.get("text") or "").strip()
        ts = event.get("ts") or ""
        if not text or not channel_id:
            return
        if not self._user_allowed(user):
            return
        # Dedup: skip if we already forwarded this exact ts.
        if ts and ts in self._seen_inbound_ts:
            return
        if ts:
            self._seen_inbound_ts.add(ts)
            # Keep the dedup set bounded.
            if len(self._seen_inbound_ts) > 500:
                self._seen_inbound_ts = set(list(self._seen_inbound_ts)[-250:])
        # Look up the session for this channel.
        for rec in self.store.records():
            if rec.channel_id == channel_id and not rec.closed:
                try:
                    self.omni.send_message(rec.session_id, text)
                    log(f"inbound: forwarded to {rec.session_id[:12]} (#{rec.channel_name})")
                except ApiError as e:
                    log(f"inbound: forward to {rec.session_id[:12]}: {e}")
                return
        log(f"inbound: no session for channel {channel_id}")

    def _user_allowed(self, user: str) -> bool:
        if not self.cfg.allowed_users:
            return user != ""
        return user in self.cfg.allowed_users

    def _set_field(self, sid: str, key: str, value: Any) -> None:
        self.store.update(lambda sess: sess.setdefault(sid, {}).__setitem__(key, value))

    # -- main loop ----------------------------------------------------

    # -- outbound: stream session updates via websocket ---------------

    def _ws_url(self) -> str:
        return self.cfg.server_url.replace("https://", "wss://").replace("http://", "ws://") + "/v1/sessions/updates"

    async def _run_websocket(self) -> None:
        """Connect to /v1/sessions/updates and process changed frames in real-time.
        Replaces the 5s polling loop — zero traffic when idle, instant on change.
        Also proactively refreshes the auth token before it expires."""
        import websockets

        # Initial watch-set: all my top-level sessions.
        sessions = self.omni.list_my_sessions()
        watched = [s["id"] for s in sessions]
        log(f"outbound: watching {len(watched)} sessions via websocket")

        headers = {"Authorization": f"Bearer {self.omni.token}"}
        async with websockets.connect(self._ws_url(), additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "watch", "session_ids": watched}))

            while True:
                try:
                    raw = await ws.recv()
                except websockets.ConnectionClosed:
                    log("outbound: websocket closed; reconnecting...")
                    await asyncio.sleep(2)
                    return  # exits this coroutine; run() will reconnect

                frame = json.loads(raw)
                ftype = frame.get("type")

                if ftype in ("snapshot", "changed"):
                    items = frame.get("items", [])
                    # Process each changed session in a thread (sync REST/Slack calls).
                    # Catch per-session errors so one bad session doesn't crash the
                    # websocket and prevent snapshot-based removal detection.
                    for item in items:
                        try:
                            await asyncio.to_thread(self._handle_session, item)
                        except Exception as e:
                            log(f"outbound: session {item.get('id','')[:12]}: {e}")
                    # Discover new session ids not in our watch-set; re-watch.
                    new_ids = [it["id"] for it in items if it.get("id") and it["id"] not in watched]
                    if new_ids:
                        watched.extend(new_ids)
                        await ws.send(json.dumps({"type": "watch", "session_ids": watched}))
                        log(f"outbound: discovered {len(new_ids)} new session(s); now watching {len(watched)}")
                    # On a full snapshot (reconnect), detect sessions that were
                    # deleted while we were disconnected: any session in our
                    # state with an open channel that's NOT in the snapshot.
                    if ftype == "snapshot":
                        snapshot_ids = {it.get("id") for it in items if it.get("id")}
                        for rec in self.store.records():
                            if not rec.channel_id:
                                continue
                            # Session not in snapshot = deleted while we were
                            # disconnected. Archive its channel.
                            if not rec.closed and rec.session_id not in snapshot_ids:
                                await asyncio.to_thread(self._handle_session_removed, rec.session_id)
                                if rec.session_id in watched:
                                    watched.remove(rec.session_id)
                            # Session marked closed in state but channel still
                            # open in Slack = a previous archive failed (e.g.
                            # rate limit during a crash loop). Retry the archive.
                            elif rec.closed and rec.session_id not in snapshot_ids:
                                await asyncio.to_thread(self._retry_archive, rec.session_id)
                elif ftype == "removed":
                    # Session was deleted — archive its Slack channel (Slack
                    # doesn't allow bot tokens to delete channels, only archive).
                    ids = frame.get("ids", [])
                    for sid in ids:
                        await asyncio.to_thread(self._handle_session_removed, sid)
                        if sid in watched:
                            watched.remove(sid)

    def run(self) -> None:
        if not self.cfg.slack_app_token:
            log("FATAL: SLACK_APP_TOKEN is required (Socket Mode for inbound).")
            log("Create a Slack app with Socket Mode enabled — see SETUP.md.")
            return
        log(f"omnigent-slack-bridge started; server={self.cfg.server_url} prefix={self.cfg.prefix} outbound=websocket inbound=socket-mode")
        self._start_socket_mode()
        if not self._socket_mode:
            log("FATAL: Socket Mode failed to connect. Check SLACK_APP_TOKEN.")
            return
        # Socket Mode handles inbound in its background thread.
        # Run the Omnigent websocket in the main asyncio loop for outbound.
        while True:
            try:
                asyncio.run(self._run_websocket())
            except Exception as e:
                log(f"outbound: websocket error: {e}; reconnecting in 5s...")
                time.sleep(5)


def _ts_greater(a: str, b: str) -> bool:
    if not a:
        return False
    if not b:
        return True
    try:
        return float(a) > float(b)
    except ValueError:
        return a > b


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────

def cmd_poll(cfg: Config) -> int:
    missing = cfg.validate()
    if missing:
        log(f"missing config: {', '.join(missing)}")
        return 1
    if not cfg.auth_token:
        log("no auth token — run `omnigent login` (or set OMNIGENT_AUTH_TOKEN)")
        return 1
    Bridge(cfg).run()


def cmd_status(cfg: Config) -> int:
    print(f"config dir: {_default_config_dir()}")
    print(f"state dir:  {cfg.state_dir}")
    print(f"server:     {cfg.server_url}")
    print(f"prefix:     {cfg.prefix}")
    print(f"private:    {cfg.private}")
    print(f"allowed:    {cfg.allowed_users or '(any non-bot user)'}")
    print(f"bot token:  {_mask(cfg.slack_bot_token)}")
    print(f"user id:     {cfg.slack_user_id or '(unset)'}")
    print(f"auth token: {_mask(cfg.auth_token)}")
    print(f"project:    {cfg.project or '(none — all my sessions)'}")
    print()
    recs = StateStore(cfg.state_dir).records()
    if not recs:
        print("sessions: (none tracked yet)")
        return 0
    print(f"sessions ({len(recs)}):")
    for r in recs:
        flag = "closed" if r.closed else "open"
        print(f"  - {r.session_id[:12]}  project={r.project}  title={r.title[:30]}  channel=#{r.channel_name}  status={r.last_status}  {flag}")
    return 0


def cmd_auth(cfg: Config) -> int:
    sc = SlackClient(cfg.slack_bot_token)
    try:
        res = sc._call("auth.test", {})
        print(f"auth.test ok={res.get('ok')} team={res.get('team')} user={res.get('user_id')} url={res.get('url')}")
    except ApiError as e:
        print(f"auth.test FAILED: {e}")
        return 1
    if cfg.server_url:
        oc = OmnigentClient(cfg)
        try:
            oc.list_my_sessions(limit=1)
            print(f"omnigent: ok (server={cfg.server_url}, token={_mask(oc.token)})")
        except ApiError as e:
            print(f"omnigent FAILED: {e}")
            return 1
    return 0


def cmd_scopes(cfg: Config) -> int:
    sc = SlackClient(cfg.slack_bot_token)
    probes = [
        ("conversations.list (channels:read)", "conversations.list", {"limit": "1", "types": "public_channel"}, False),
        ("conversations.create (channels:manage)", "conversations.create", {"name": "__omni_scope_probe__"}, True),
        ("conversations.invite (invites:write)", "conversations.invite", {"channel": "__none__", "users": cfg.slack_user_id}, True),
        ("conversations.setTopic (channels:manage)", "conversations.setTopic", {"channel": "__none__", "topic": "x"}, True),
    ]
    for name, method, params, post in probes:
        try:
            sc._call(method, params, post=post)
            print(f"{name:45s} OK")
        except ApiError as e:
            # For create/invite, a scope-missing error is 'missing_scope';
            # a validation error (e.g. name_taken, channel_not_found) means
            # the scope IS present — the call just had a bad arg.
            if e.err in ("name_taken", "channel_not_found", "already_in_channel"):
                print(f"{name:45s} OK (scope present)")
            else:
                print(f"{name:45s} {e.err}")
    return 0


def cmd_verify(cfg: Config) -> int:
    sc = SlackClient(cfg.slack_bot_token)
    recs = StateStore(cfg.state_dir).records()
    if not recs:
        print("no sessions tracked")
        return 1
    for r in recs:
        print(f"\n{r.session_id[:12]} -> #{r.channel_name} id={r.channel_id} status={r.last_status} closed={r.closed}")
        if not r.channel_id or r.closed:
            print("  (no channel / closed)")
            continue
        try:
            res = sc._call("conversations.info", {"channel": r.channel_id}, post=False)
            ch = res.get("channel", {})
            print(f"  slack: name=#{ch.get('name')} archived={ch.get('is_archived')} topic={ch.get('topic',{}).get('value','')[:50]}")
        except ApiError as e:
            print(f"  conversations.info FAILED: {e}")


def cmd_find(cfg: Config) -> int:
    sc = SlackClient(cfg.slack_bot_token)
    cursor = ""
    total = 0
    for _ in range(20):
        res = sc._call("conversations.list", {"limit": "200", "types": "public_channel", "cursor": cursor}, post=False)
        for ch in res.get("channels") or []:
            total += 1
            name = ch.get("name") or ""
            if name.startswith(cfg.prefix + "-") or ch.get("is_archived"):
                print(f"  #{name} id={ch.get('id')} archived={ch.get('is_archived')}")
        cursor = (res.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    print(f"total visible: {total}")
    return 0


def cmd_channel_op(cfg: Config, op: str, channel_id: str) -> int:
    sc = SlackClient(cfg.slack_bot_token)
    try:
        if op == "archive":
            sc.archive(channel_id)
        else:
            sc.unarchive(channel_id)
        print(f"{op} {channel_id} OK")
        return 0
    except ApiError as e:
        print(f"{op} {channel_id} FAILED: {e}")
        return 1


def cmd_config_dir(_: Config) -> int:
    print(_default_config_dir())
    return 0


def cmd_state_dir(cfg: Config) -> int:
    print(cfg.state_dir)
    return 0


USAGE = """omnigent-slack-bridge — per-agent Slack channels for Omnigent sessions

Usage:
  omnigent-slack-bridge poll          Run the bridge daemon (outbound + inbound).
  omnigent-slack-bridge status        Print config + tracked sessions.
  omnigent-slack-bridge auth          Probe Slack auth.test + Omnigent list.
  omnigent-slack-bridge scopes        Probe which Slack scopes the token has.
  omnigent-slack-bridge verify        Show recent messages in each channel.
  omnigent-slack-bridge find          List channels matching the prefix.
  omnigent-slack-bridge archive <id>  Archive a channel by id.
  omnigent-slack-bridge unarchive <id>  Unarchive a channel by id.
  omnigent-slack-bridge config-dir   Print the user config directory.
  omnigent-slack-bridge state-dir     Print the state directory.

Config (env file then environment):
  ~/.config/omnigent-slack-bridge/config.env

Required:
  SLACK_BOT_TOKEN=xoxb-...
  SLACK_APP_TOKEN=xapp-...   (Socket Mode — see SETUP.md)
  OMNIGENT_SLACK_BRIDGE_PREFIX=ck
  (OMNIGENT_SERVER_URL + token are auto-read from ~/.omnigent by default)

Optional:
  SLACK_USER_ID=U...                   (pinged once on first blocked)
  OMNIGENT_SLACK_BRIDGE_PRIVATE=true   (private channels)
  OMNIGENT_SLACK_BRIDGE_ALLOWED_USERS=U1,U2
  OMNIGENT_SLACK_BRIDGE_PROJECT=<name> (scope to one Omnigent project)
  OMNIGENT_AUTH_TOKEN=<jwt>            (override ~/.omnigent/auth_tokens.json)
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="omnigent-slack-bridge", add_help=False)
    parser.add_argument("command")
    parser.add_argument("rest", nargs="*")
    args = parser.parse_args(argv)
    cfg = load_config()
    cmd = args.command
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if cmd == "poll":
        return cmd_poll(cfg)
    if cmd == "status":
        return cmd_status(cfg)
    if cmd == "auth":
        return cmd_auth(cfg)
    if cmd == "scopes":
        return cmd_scopes(cfg)
    if cmd == "verify":
        return cmd_verify(cfg)
    if cmd == "find":
        return cmd_find(cfg)
    if cmd in ("archive", "unarchive"):
        if not args.rest:
            print(f"usage: omnigent-slack-bridge {cmd} <channel-id>", file=sys.stderr)
            return 2
        return cmd_channel_op(cfg, cmd, args.rest[0])
    if cmd == "config-dir":
        return cmd_config_dir(cfg)
    if cmd == "state-dir":
        return cmd_state_dir(cfg)
    print(f"unknown command: {cmd}\n", file=sys.stderr)
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

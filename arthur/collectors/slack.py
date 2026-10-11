"""Slack DMs + @mentions via a user token (xoxp-) per workspace.

Token scopes: search:read, users:read. Put tokens in config.json -> slack.workspaces: [{"name","token"}].
Uses one search.messages sweep per workspace, so it stays well inside rate limits.
"""
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import requests

from . import error, offline, ok

API = "https://slack.com/api"


def _call(token, method, **params):
    r = requests.get(f"{API}/{method}", headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30).json()
    if not r.get("ok"):
        raise RuntimeError(f"{method}: {r.get('error')}")
    return r


def collect(cfg, since: datetime) -> dict:
    wss = [w for w in cfg["slack"]["workspaces"] if w.get("token")]
    if not wss:
        return offline("Slack not linked (needs user token)")
    items, errs, stats = [], [], {"scanned": 0, "dms": 0, "mentions": 0, "answered": 0}
    after = (since.astimezone() - timedelta(days=1)).strftime("%Y-%m-%d")
    for ws in wss:
        try:
            me = _call(ws["token"], "auth.test")
            uid, team_url = me["user_id"], me.get("url", "")
            msgs = []
            for page in (1, 2, 3):
                r = _call(ws["token"], "search.messages", query=f"after:{after}", sort="timestamp",
                          sort_dir="desc", count=100, page=page)
                m = r["messages"]
                msgs += m.get("matches", [])
                if page >= m.get("paging", {}).get("pages", 1):
                    break
            # my own messages, per channel, so we can tell what I've already answered
            mine = {}
            for m in msgs:
                if m.get("user") == uid:
                    mine.setdefault(m.get("channel", {}).get("id"), []).append(float(m["ts"]))
            for m in msgs:
                stats["scanned"] += 1
                ts = datetime.fromtimestamp(float(m["ts"])).astimezone()
                if ts < since or m.get("user") == uid:
                    continue
                ch = m.get("channel", {})
                text = m.get("text", "")
                is_dm = bool(ch.get("is_im") or ch.get("is_mpim"))
                mentioned = f"<@{uid}>" in text
                if not (is_dm or mentioned):  # only DMs and direct tags; @channel/@here are ignored
                    continue
                if _answered(ws["token"], uid, ch.get("id"), m, mine, is_dm):
                    stats["answered"] = stats.get("answered", 0) + 1
                    continue
                flags = ["dm", "awaiting-reply"] if is_dm else ["mention", "awaiting-reply"]
                stats["dms" if is_dm else "mentions"] += 1
                where = "DM" if ch.get("is_im") else ("Group DM" if ch.get("is_mpim") else f"#{ch.get('name', '?')}")
                items.append({
                    "id": f"sl:{ws.get('name', 'ws')}:{ch.get('id')}:{m['ts']}",
                    "source": "slack",
                    "account": ws.get("name") or team_url,
                    "kind": "dm" if is_dm else "mention",
                    "from": m.get("username", "?"),
                    "title": where,
                    "text": re.sub(r"<@(\w+)>", lambda x: "@you" if x.group(1) == uid else "@someone", text)[:400],
                    "time": ts.isoformat(),
                    "link": m.get("permalink", team_url),
                    "flags": flags,
                    "meta": {"workspace": ws.get("name", "ws"), "channel": ch.get("id"), "ts": m["ts"], "dm": is_dm,
                             "thread_ts": _thread_ts(m)},
                })
        except Exception as e:  # noqa: BLE001
            errs.append(f"{ws.get('name', '?')}: {str(e)[:150]}")
    if not items and errs:
        return error("; ".join(errs))
    return ok(items, stats, "; ".join(errs))


def _thread_ts(m) -> str | None:
    q = parse_qs(urlparse(m.get("permalink", "")).query)
    return (q.get("thread_ts") or [None])[0]


def _answered(token, uid, channel, m, mine, is_dm) -> bool:
    """True if I've already replied after this message (DM: anything later from me; channel: in its thread or later in the channel)."""
    t = float(m["ts"])
    if any(x > t for x in mine.get(channel, [])):
        return True
    if is_dm:
        return False
    try:
        thread = _thread_ts(m) or m["ts"]
        replies = _call(token, "conversations.replies", channel=channel, ts=thread, limit=100).get("messages", [])
        return any(r.get("user") == uid and float(r["ts"]) > t for r in replies)
    except RuntimeError:
        return False


def send(cfg, workspace: str, channel: str, text: str, thread_ts: str | None = None) -> dict:
    """Post as the user. Only ever called from an explicit Send click in the HUD."""
    ws = next((w for w in cfg["slack"]["workspaces"] if w.get("name") == workspace and w.get("token")), None)
    if not ws:
        raise RuntimeError(f"no token for workspace {workspace!r}")
    body = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    r = requests.post(f"{API}/chat.postMessage", headers={"Authorization": f"Bearer {ws['token']}"}, json=body, timeout=30).json()
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "send failed"))
    return {"ok": True, "ts": r.get("ts")}

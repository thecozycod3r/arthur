"""Hotmail / Outlook.com via Microsoft Graph (Mail.Read, delegated, device-code login).

Needs an Azure app registration's client ID in config.json -> outlook.client_id.
Link with:  python -m arthur auth outlook
"""
import time
from datetime import datetime, timezone

import requests

from ..config import SECRETS, read_json, write_json
from . import error, offline, ok

AUTH = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPE = "offline_access Mail.Read User.Read"
TOKEN = SECRETS / "outlook.json"


def link_account(client_id: str) -> str:
    r = requests.post(f"{AUTH}/devicecode", data={"client_id": client_id, "scope": SCOPE}).json()
    if "user_code" not in r:
        raise RuntimeError(r)
    print(f"\n  Open {r['verification_uri']} and enter code:  {r['user_code']}\n")
    while True:
        time.sleep(r.get("interval", 5))
        t = requests.post(f"{AUTH}/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id, "device_code": r["device_code"],
        }).json()
        if "access_token" in t:
            _store(t)
            me = requests.get("https://graph.microsoft.com/v1.0/me",
                              headers={"Authorization": f"Bearer {t['access_token']}"}).json()
            return me.get("userPrincipalName") or me.get("mail") or "?"
        if t.get("error") not in ("authorization_pending", "slow_down"):
            raise RuntimeError(t)


def _store(t):
    t["expires_at"] = time.time() + int(t.get("expires_in", 3600)) - 60
    write_json(TOKEN, t)


def _access_token(client_id: str) -> str | None:
    t = read_json(TOKEN)
    if not t:
        return None
    if time.time() < t.get("expires_at", 0):
        return t["access_token"]
    n = requests.post(f"{AUTH}/token", data={
        "grant_type": "refresh_token", "client_id": client_id,
        "refresh_token": t["refresh_token"], "scope": SCOPE,
    }).json()
    if "access_token" not in n:
        raise RuntimeError(f"refresh failed: {n.get('error_description', n)[:200]}")
    n.setdefault("refresh_token", t["refresh_token"])
    _store(n)
    return n["access_token"]


def collect(cfg, since: datetime) -> dict:
    o = cfg["outlook"]
    if not o.get("client_id"):
        return offline("Hotmail not linked (needs Azure client ID)")
    try:
        tok = _access_token(o["client_id"])
        if not tok:
            return offline("Hotmail not linked — run: python -m arthur auth outlook")
        h = {"Authorization": f"Bearer {tok}"}
        me = requests.get("https://graph.microsoft.com/v1.0/me", headers=h).json()
        acct = me.get("userPrincipalName") or me.get("mail") or "hotmail"
        iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = requests.get(
            "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
            headers=h,
            params={
                "$filter": f"receivedDateTime ge {iso}",
                "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead,importance,inferenceClassification,webLink,flag",
                "$orderby": "receivedDateTime desc",
                "$top": 60,
            },
        )
        if r.status_code >= 400:
            return error(f"Graph {r.status_code}: {r.text[:200]}")
        items, other = [], 0
        for m in r.json().get("value", []):
            if m.get("inferenceClassification") == "other":
                other += 1
                continue
            f = m.get("from", {}).get("emailAddress", {})
            flags = []
            if not m.get("isRead"):
                flags.append("unread")
            if m.get("importance") == "high":
                flags.append("important")
            if m.get("flag", {}).get("flagStatus") == "flagged":
                flags.append("starred")
            items.append({
                "id": f"ol:{m['id'][-24:]}",
                "source": "hotmail",
                "account": acct,
                "kind": "email",
                "from": f"{f.get('name', '')} <{f.get('address', '')}>",
                "title": m.get("subject") or "(no subject)",
                "text": (m.get("bodyPreview") or "")[:400],
                "time": m["receivedDateTime"],
                "link": m.get("webLink", "https://outlook.live.com/mail/"),
                "flags": flags,
            })
        return ok(items, {"scanned": len(items) + other, "unread": sum("unread" in i["flags"] for i in items),
                          "skipped_promos": other})
    except Exception as e:  # noqa: BLE001
        return error(str(e)[:200])

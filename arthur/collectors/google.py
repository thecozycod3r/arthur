"""Gmail + Google Calendar, read-only, any number of Google accounts.

Tokens: secrets/google_<label>.json. Link an account with:  python -m arthur auth google <label>
"""
from datetime import datetime, timedelta

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

from ..config import SECRETS
from . import error, offline, ok

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # drafts + sending replies you approve
    "https://www.googleapis.com/auth/drive.readonly",  # search/open files only — cannot edit or delete
    "https://www.googleapis.com/auth/calendar.events",  # create / move events you approve (never deletes)
]
CLIENT = SECRETS / "google_client.json"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"


def token_path(label: str):
    return SECRETS / f"google_{label}.json"


def link_account(label: str) -> str:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    token_path(label).write_text(creds.to_json())
    sess = AuthorizedSession(creds)
    return _get(sess, f"{GMAIL}/profile").get("emailAddress", "?")


def _session(label: str):
    p = token_path(label)
    if not p.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(p))
    if not creds.valid:
        creds.refresh(Request())
        p.write_text(creds.to_json())
    return AuthorizedSession(creds)


def _get(s, url, params=None):
    r = s.get(url, params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:300]}")
    return r.json()


def _hdr(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def gmail(cfg, since: datetime) -> dict:
    labels = cfg["google_accounts"]
    if not labels:
        return offline("No Google account linked")
    items, stats, errs = [], {"scanned": 0, "unread": 0, "skipped_promos": 0}, []
    hours = max(1, int((datetime.now(since.tzinfo) - since).total_seconds() // 3600))
    for label in labels:
        try:
            s = _session(label)
            if s is None:
                errs.append(f"{label}: not linked")
                continue
            email = _get(s, f"{GMAIL}/profile").get("emailAddress", label)
            q = f"newer_than:{max(1, hours // 24)}d -in:sent -in:chats -category:promotions -category:social -category:forums"
            total = _get(s, f"{GMAIL}/messages", params={"q": f"newer_than:{max(1, hours // 24)}d category:promotions", "maxResults": 1})
            stats["skipped_promos"] += total.get("resultSizeEstimate", 0)
            ids = _get(s, f"{GMAIL}/messages", params={"q": q, "maxResults": 60}).get("messages", [])
            for m in ids:
                d = _get(s,
                    f"{GMAIL}/messages/{m['id']}",
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date", "To", "Cc"]},
                )
                ts = datetime.fromtimestamp(int(d.get("internalDate", "0")) / 1000).astimezone()
                if ts < since:
                    continue
                lab = d.get("labelIds", [])
                h = d.get("payload", {}).get("headers", [])
                flags = [f.lower() for f in ("UNREAD", "IMPORTANT", "STARRED") if f in lab]
                if email.lower() not in (_hdr(h, "To") or "").lower() and email.lower() in (_hdr(h, "Cc") or "").lower():
                    flags.append("cc")
                stats["scanned"] += 1
                stats["unread"] += "unread" in flags
                thread = d.get("threadId", m["id"])
                if any(x["id"] == f"gm:{label}:{thread}" for x in items):
                    continue  # newest message of the thread already kept (list is newest-first)
                items.append({
                    "id": f"gm:{label}:{thread}",
                    "source": "gmail",
                    "account": email,
                    "kind": "email",
                    "from": _hdr(h, "From"),
                    "title": _hdr(h, "Subject") or "(no subject)",
                    "text": d.get("snippet", "")[:400],
                    "time": ts.isoformat(),
                    "link": f"https://mail.google.com/mail/?authuser={email}#all/{d.get('threadId', m['id'])}",
                    "flags": flags,
                    "meta": {"label": label, "thread": d.get("threadId"), "msg": m["id"]},
                })
        except Exception as e:  # noqa: BLE001 — one bad account must not sink the briefing
            errs.append(f"{label}: {_explain(e)}")
    if not items and errs:
        return error("; ".join(errs))
    return ok(items, stats, "; ".join(errs))


def calendar(cfg, since: datetime) -> dict:
    labels = cfg["google_accounts"]
    if not labels:
        return offline("No Google account linked")
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2)
    items, errs, seen = [], [], set()
    for label in labels:
        try:
            s = _session(label)
            if s is None:
                errs.append(f"{label}: not linked")
                continue
            cals = _get(s, f"{CAL}/users/me/calendarList", params={"minAccessRole": "reader"}).get("items", [])
            cals = [c for c in cals if c.get("selected") or c.get("primary")]
            for c in cals:
                try:
                    evs = _get(s,
                        f"{CAL}/calendars/{c['id']}/events",
                        params={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                                "singleEvents": "true", "orderBy": "startTime", "maxResults": 50},
                    ).get("items", [])
                except RuntimeError:
                    continue  # calendars you can list but not read (holidays, revoked shares)
                for e in evs:
                    if e.get("status") == "cancelled":
                        continue
                    me = next((a for a in e.get("attendees", []) if a.get("self")), {})
                    if me.get("responseStatus") == "declined":
                        continue
                    key = (e.get("summary"), e["start"].get("dateTime") or e["start"].get("date"))
                    if key in seen:
                        continue
                    seen.add(key)
                    all_day = "date" in e["start"]
                    items.append({
                        "id": f"cal:{label}:{e['id']}",
                        "source": "calendar",
                        "account": c.get("summary", label),
                        "kind": "event",
                        "from": e.get("organizer", {}).get("email", ""),
                        "title": e.get("summary", "(busy)"),
                        "text": (e.get("description") or "")[:300],
                        "time": e["start"].get("dateTime") or e["start"].get("date"),
                        "end": e["end"].get("dateTime") or e["end"].get("date"),
                        "all_day": all_day,
                        "location": e.get("location", ""),
                        "meet": e.get("hangoutLink", ""),
                        "attendees": len(e.get("attendees", [])),
                        "rsvp": me.get("responseStatus", ""),
                        "link": e.get("htmlLink", ""),
                        "flags": ["needs-rsvp"] if me.get("responseStatus") == "needsAction" else [],
                    })
        except Exception as e:  # noqa: BLE001
            errs.append(f"{label}: {_explain(e)}")
    items.sort(key=lambda x: x["time"])
    if not items and errs:
        return error("; ".join(errs))
    return ok(items, {"events": len(items)}, "; ".join(errs))


def _explain(e: Exception) -> str:
    msg = str(e)
    if "accessNotConfigured" in msg or "has not been used in project" in msg:
        return "API not enabled on the OAuth project — enable Gmail/Calendar API in Google Cloud console"
    if "invalid_grant" in msg:
        return "token expired — re-run auth"
    return msg[:200]


def can_compose(label: str) -> bool:
    p = token_path(label)
    return p.exists() and "gmail.compose" in p.read_text()


def reply(label: str, msg_id: str, body: str, send: bool = False) -> dict:
    """Draft (or send) a reply in the same Gmail thread. Only called after you approve it."""
    import base64
    from email.message import EmailMessage

    if not can_compose(label):
        raise RuntimeError("Gmail drafting not allowed yet — run: python -m arthur auth google " + label)
    s = _session(label)
    m = _get(s, f"{GMAIL}/messages/{msg_id}", params={"format": "metadata",
             "metadataHeaders": ["From", "Reply-To", "Subject", "Message-ID", "References"]})
    h = m.get("payload", {}).get("headers", [])
    me = _get(s, f"{GMAIL}/profile").get("emailAddress")
    subj = _hdr(h, "Subject")
    em = EmailMessage()
    em["To"] = _hdr(h, "Reply-To") or _hdr(h, "From")
    em["From"] = me
    em["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    if _hdr(h, "Message-ID"):
        em["In-Reply-To"] = _hdr(h, "Message-ID")
        em["References"] = f"{_hdr(h, 'References')} {_hdr(h, 'Message-ID')}".strip()
    em.set_content(body)
    raw = base64.urlsafe_b64encode(em.as_bytes()).decode()
    msg = {"raw": raw, "threadId": m.get("threadId")}
    if send:
        r = s.post(f"{GMAIL}/messages/send", json=msg)
    else:
        r = s.post(f"{GMAIL}/drafts", json={"message": msg})
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    return {"ok": True, "sent": send, "to": em["To"]}


def drive_search(query: str, limit: int = 5) -> list[dict]:
    """Read-only Drive search across linked accounts (name match first, then full text)."""
    out = []
    q_safe = query.replace("\\", "").replace("'", "\\'")
    for label in __import__("arthur.config", fromlist=["load"]).load()["google_accounts"]:
        p = token_path(label)
        if not p.exists() or "drive.readonly" not in p.read_text():
            continue
        s = _session(label)
        for q in (f"name contains '{q_safe}' and trashed = false", f"fullText contains '{q_safe}' and trashed = false"):
            files = _get(s, "https://www.googleapis.com/drive/v3/files", params={
                "q": q, "pageSize": limit, "orderBy": "modifiedTime desc", "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true", "fields": "files(id,name,mimeType,modifiedTime,webViewLink,owners(displayName))",
            }).get("files", [])
            for f in files:
                if f["id"] not in {x["id"] for x in out}:
                    out.append({**f, "account": label})
            if len(out) >= limit:
                break
    return out[:limit]

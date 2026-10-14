"""Things Arthur can do on your Mac and in your accounts beyond replying:
memory, reminders, timers, calls, music, new emails / iMessages, calendar create / move, and an action log
(feeds the evening wrap-up). Anything outward (call, send, invite) is only reached after your yes."""
import json
import re
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta

from . import config

LOG = config.DATA / "actions.jsonl"
MEMORY = config.DATA / "memory.json"
TIMERS = config.DATA / "timers.json"


def log_action(kind: str, **detail):
    """Append-only record of what Arthur did today (for the evening wrap-up)."""
    with open(LOG, "a") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, **detail}) + "\n")


def actions_today() -> list[dict]:
    if not LOG.exists():
        return []
    today = datetime.now().date().isoformat()
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.startswith('{"at": "' + today)]


def _osa(script: str, *args) -> str:
    p = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[:200] or "AppleScript failed")
    return p.stdout.strip()


# ---------- memory (never deleted: "forget" archives) ----------
def memories(active_only=True) -> list[dict]:
    m = config.read_json(MEMORY, []) or []
    return [x for x in m if not x.get("archived")] if active_only else m


def remember(fact: str) -> dict:
    m = config.read_json(MEMORY, []) or []
    m.append({"fact": fact.strip(), "added": datetime.now().isoformat(timespec="seconds")})
    config.write_json(MEMORY, m)
    return {"ok": True, "count": len([x for x in m if not x.get("archived")])}


def forget(query: str) -> dict:
    """Archive (not delete) the memories that best match `query`."""
    from difflib import SequenceMatcher
    m = config.read_json(MEMORY, []) or []
    active = [x for x in m if not x.get("archived")]
    if not active:
        return {"ok": False, "error": "I don't have anything remembered yet"}
    best = max(active, key=lambda x: SequenceMatcher(a=query.lower(), b=x["fact"].lower()).ratio())
    best["archived"] = datetime.now().isoformat(timespec="seconds")
    config.write_json(MEMORY, m)
    return {"ok": True, "forgot": best["fact"]}


# ---------- reminders (Apple Reminders; syncs to your phone) ----------
def add_reminder(title: str, when_iso: str | None) -> dict:
    if when_iso:
        t = datetime.fromisoformat(when_iso)
        script = ('on run argv\n tell application "Reminders"\n'
                  '  set d to current date\n'
                  '  set year of d to (item 2 of argv as integer)\n  set month of d to (item 3 of argv as integer)\n'
                  '  set day of d to (item 4 of argv as integer)\n'
                  '  set time of d to ((item 5 of argv as integer) * 3600 + (item 6 of argv as integer) * 60)\n'
                  '  make new reminder with properties {name:(item 1 of argv), remind me date:d}\n'
                  ' end tell\nend run')
        _osa(script, title, str(t.year), str(t.month), str(t.day), str(t.hour), str(t.minute))
    else:
        _osa('on run argv\n tell application "Reminders" to make new reminder with properties {name:(item 1 of argv)}\nend run', title)
    log_action("reminder", title=title, when=when_iso)
    return {"ok": True}


# ---------- timers (Arthur speaks when they're up; survive restarts) ----------
_timer_cb = None


def start_timers(on_fire):
    """Called by the server at start: re-arm saved timers. on_fire(label) speaks."""
    global _timer_cb
    _timer_cb = on_fire
    for t in config.read_json(TIMERS, []) or []:
        _arm(t)


def _arm(t: dict):
    delay = max(0.0, t["due"] - time.time())

    def fire():
        timers = [x for x in (config.read_json(TIMERS, []) or []) if x["id"] != t["id"]]
        config.write_json(TIMERS, timers)
        if _timer_cb:
            _timer_cb(t["label"])
    threading.Timer(delay, fire).start()


def set_timer(seconds: int, label: str) -> dict:
    if int(seconds) <= 0:
        return {"ok": False, "error": "I didn't catch how long"}
    t = {"id": f"t{int(time.time() * 1000)}", "label": label or "your", "due": time.time() + int(seconds)}
    timers = (config.read_json(TIMERS, []) or []) + [t]
    config.write_json(TIMERS, timers)
    _arm(t)  # timers aren't logged as "done" — not worth a line in the wrap-up
    return {"ok": True, "ends": datetime.fromtimestamp(t["due"]).strftime("%-I:%M %p").lower()}


# ---------- people -> phone / email ----------
def find_phone(name: str) -> dict:
    """Name -> phone number via WhatsApp's contact list (has numbers for your contacts)."""
    from .collectors import whatsapp
    hits = whatsapp.find(name)
    best = [h for h in hits if h["score"] >= 0.95]
    if len(best) == 1 or (len(hits) == 1 and not best):
        h = (best or hits)[0]
        return {"ok": True, "name": h["name"], "phone": "+" + h["jid"].split("@")[0]}
    if hits:
        return {"ok": False, "ambiguous": [h["name"] for h in (best or hits)]}
    return {"ok": False, "error": f"no contact called {name}"}


def find_email(name: str) -> dict:
    """Name -> email address, from who you've actually emailed with (Gmail headers)."""
    from .collectors import google
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", name.strip(), re.I):
        return {"ok": True, "name": name.strip(), "email": name.strip()}
    label = (config.load().get("google_accounts") or ["work"])[0]
    s = google._session(label)
    ids = google._get(s, f"{google.GMAIL}/messages", params={"q": f'"{name}" newer_than:2y', "maxResults": 25}).get("messages", [])
    found = Counter()
    names = {}
    want = name.lower().split()[0]
    for m in ids[:25]:
        d = google._get(s, f"{google.GMAIL}/messages/{m['id']}", params={"format": "metadata", "metadataHeaders": ["From", "To", "Cc"]})
        for h in d.get("payload", {}).get("headers", []):
            for disp, addr in re.findall(r'"?([^",<]*?)"?\s*<([^>]+)>', h["value"]):
                if want in disp.lower() or want in addr.lower().split("@")[0]:
                    found[addr.lower()] += 1
                    names[addr.lower()] = disp.strip() or addr
    if not found:
        return {"ok": False, "error": f"I couldn't find an email address for {name}"}
    top = found.most_common(3)
    if len(top) > 1 and top[1][1] >= top[0][1] * 0.6:
        return {"ok": False, "ambiguous": [f"{names[a]} <{a}>" for a, _ in top]}
    a = top[0][0]
    return {"ok": True, "name": names[a], "email": a}


# ---------- calls (rings through your iPhone via FaceTime / Continuity) ----------
def call(name: str) -> dict:
    p = find_phone(name)
    if not p.get("ok"):
        return p
    subprocess.run(["open", f"tel://{p['phone']}"], check=True)
    log_action("call", to=p["name"])
    return {"ok": True, "name": p["name"]}


# ---------- music (Apple Music) ----------
def music(command: str, query: str = "", level: int | None = None) -> dict:
    c = (command or "").lower()
    simple = {"play": "play", "resume": "play", "pause": "pause", "stop": "pause", "next": "next track",
              "skip": "next track", "previous": "previous track", "back": "previous track"}
    if c in simple and not query:
        _osa(f'tell application "Music" to {simple[c]}')
    elif c == "volume" and level is not None:
        _osa(f'tell application "Music" to set sound volume to {max(0, min(100, int(level)))}')
    elif c in ("playlist", "play") and query:
        try:
            _osa('on run argv\n tell application "Music" to play playlist (item 1 of argv)\nend run', query)
        except RuntimeError:
            _osa('on run argv\n tell application "Music"\n'
                 '  set r to (every track of playlist "Library" whose name contains (item 1 of argv) or artist contains (item 1 of argv))\n'
                 '  if r is {} then error "nothing in your library matches"\n  play item 1 of r\n end tell\nend run', query)
    elif c == "now":
        return {"ok": True, "playing": _osa('tell application "Music" to get (name of current track) & " by " & (artist of current track)')}
    else:
        return {"ok": False, "error": "I didn't catch what to do with the music"}
    return {"ok": True}


# ---------- new emails / iMessages (after your yes) ----------
def email_new(to: str, subject: str, body: str, send: bool) -> dict:
    import base64
    from email.message import EmailMessage
    from .collectors import google
    r = find_email(to)
    if not r.get("ok"):
        return r
    label = (config.load().get("google_accounts") or ["work"])[0]
    if not google.can_compose(label):
        return {"ok": False, "error": "Gmail sending isn't allowed yet"}
    s = google._session(label)
    me = google._get(s, f"{google.GMAIL}/profile").get("emailAddress")
    em = EmailMessage()
    em["To"], em["From"], em["Subject"] = r["email"], me, subject or "(no subject)"
    em.set_content(body)
    msg = {"raw": base64.urlsafe_b64encode(em.as_bytes()).decode()}
    resp = s.post(f"{google.GMAIL}/messages/send", json=msg) if send else s.post(f"{google.GMAIL}/drafts", json={"message": msg})
    if resp.status_code >= 400:
        return {"ok": False, "error": resp.text[:200]}
    log_action("email_sent" if send else "email_drafted", to=r["email"], subject=subject)
    return {"ok": True, "to": r["email"], "sent": send}


def imessage_new(to: str, text: str) -> dict:
    from .collectors import imessage
    p = find_phone(to)
    if not p.get("ok"):
        return p
    imessage.send(p["phone"], text)
    log_action("imessage_sent", to=p["name"])
    return {"ok": True, "name": p["name"]}


# ---------- calendar: create / move (never delete) ----------
def _cal():
    from .collectors import google
    label = (config.load().get("google_accounts") or ["work"])[0]
    p = google.token_path(label)
    if "calendar.events" not in p.read_text():
        raise RuntimeError("calendar editing isn't allowed yet — approve the Google permission")
    return google._session(label)


def event_create(title: str, start_iso: str, minutes: int = 30, attendees: list[str] | None = None,
                 location: str = "") -> dict:
    s = _cal()
    start = datetime.fromisoformat(start_iso)
    if start.tzinfo is None:
        start = start.astimezone()
    end = start + timedelta(minutes=minutes or 30)
    emails, missing = [], []
    for a in attendees or []:
        r = find_email(a)
        (emails.append(r["email"]) if r.get("ok") else missing.append(a))
    body = {"summary": title, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
    if location:
        body["location"] = location
    if emails:
        body["attendees"] = [{"email": e} for e in emails]
    r = s.post("https://www.googleapis.com/calendar/v3/calendars/primary/events",
               params={"sendUpdates": "all" if emails else "none"}, json=body)
    if r.status_code >= 400:
        return {"ok": False, "error": r.text[:200]}
    log_action("event_created", title=title, start=start.isoformat(), invited=emails)
    return {"ok": True, "invited": emails, "not_found": missing, "link": r.json().get("htmlLink")}


def event_move(event_id: str, start_iso: str, minutes: int | None = None) -> dict:
    s = _cal()
    eid = event_id.split(":", 2)[-1]  # "cal:work:<id>" -> "<id>"
    cur = s.get(f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{eid}").json()
    if "start" not in cur:
        return {"ok": False, "error": "I couldn't find that event"}
    old_s = datetime.fromisoformat(cur["start"].get("dateTime", cur["start"].get("date")))
    old_e = datetime.fromisoformat(cur["end"].get("dateTime", cur["end"].get("date")))
    start = datetime.fromisoformat(start_iso)
    if start.tzinfo is None:
        start = start.astimezone()
    end = start + (timedelta(minutes=minutes) if minutes else (old_e - old_s))
    r = s.patch(f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{eid}",
                params={"sendUpdates": "all" if cur.get("attendees") else "none"},
                json={"start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}})
    if r.status_code >= 400:
        return {"ok": False, "error": r.text[:200]}
    log_action("event_moved", title=cur.get("summary"), to=start.isoformat())
    return {"ok": True, "title": cur.get("summary")}

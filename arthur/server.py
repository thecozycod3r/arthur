"""Local HUD server — http://localhost:8730 (bound to 127.0.0.1 only)."""
import logging
import subprocess
import threading
import time
from datetime import date, datetime

import hmac

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import actions, config, pipeline, voice

app = FastAPI(title="Arthur", docs_url=None, redoc_url=None, openapi_url=None)
TOKEN = config.api_token()
PORT = config.load()["port"]
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}


SENSITIVE = ("/api/slack/send", "/api/email/reply", "/api/imessage/send", "/api/whatsapp/send",
             "/api/claude/session", "/api/claude/task",
             "/api/do/call", "/api/do/email_new", "/api/do/imessage_new", "/api/do/event_create", "/api/do/event_move")


def screen_locked() -> bool:
    """Set/cleared by the menu bar app from macOS's live lock/unlock events."""
    return (config.DATA / "screen_locked").exists()


@app.middleware("http")
async def guard(request: Request, call_next):
    """Only this Mac, only Arthur's own pages/processes.
    - Host check: blocks DNS-rebinding (a web page pretending to be localhost).
    - Origin check: blocks other websites posting to Arthur from your browser.
    - Token: every API call needs the secret (header from Arthur's processes, SameSite=Strict cookie from the HUD)."""
    if request.headers.get("host", "") not in ALLOWED_HOSTS:
        return JSONResponse({"error": "forbidden host"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"error": "forbidden origin"}, status_code=403)
    path = request.url.path
    if path.startswith("/api/"):
        got = request.headers.get("x-arthur-token") or request.cookies.get("arthur_token") or ""
        if not hmac.compare_digest(got, TOKEN):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
    if path in SENSITIVE and screen_locked():
        return JSONResponse({"ok": False, "error": "your Mac is locked — unlock it to send or run anything"}, status_code=423)
    resp = await call_next(request)
    if path == "/":
        resp.set_cookie("arthur_token", TOKEN, httponly=True, samesite="strict", path="/")
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _janitor():
    while True:
        time.sleep(60)
        voice.unload_if_idle()


threading.Thread(target=_janitor, daemon=True).start()


def _timer_done(label: str):
    subprocess.run(["osascript", "-e", f'display notification "{label} timer is done" with title "Arthur" sound name "Glass"'])
    speak(Speech(text=f"Sir, your {label} timer is done." if label and label != "your" else "Sir, your timer is done."))


actions.start_timers(_timer_done)
_lock = threading.Lock()
_say: subprocess.Popen | None = None
_speech = {"started": 0.0, "duration": 0.0}
_queue: list = []  # everything queued to say, so Stop can silence all of it
DONE = config.DATA / "done.json"


@app.get("/")
def index():
    return FileResponse(config.UI / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/briefing")
def briefing():
    b = config.read_json(pipeline.BRIEFING)
    return JSONResponse(b or {"empty": True}, headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    return config.read_json(pipeline.STATUS, {"phase": "idle", "message": "Standing by"})


@app.post("/api/refresh")
def refresh():
    if not _lock.acquire(blocking=False):
        return {"started": False, "reason": "already running"}

    def go():
        try:
            pipeline.safe_run()
        finally:
            _lock.release()

    threading.Thread(target=go, daemon=True).start()
    return {"started": True}


class Speech(BaseModel):
    text: str | None = None
    stream: bool = False     # more text will follow via /api/speak/append
    interrupt: bool = False  # cut off whatever he's saying (default: queue after it)


@app.post("/api/speak")
def speak(body: Speech):
    global _say
    prev = _say if (_say is not None and _say.poll() is None) else None
    if body.interrupt:
        stop()
        prev = None
    text = body.text
    if not text:
        b = config.read_json(pipeline.BRIEFING) or {}
        text = b.get("brief", {}).get("spoken_briefing", "No briefing yet.")
    _say, dur = voice.speak(text, stream=body.stream, after=prev if isinstance(prev, voice.SpeechJob) else None)
    _queue.append(_say)
    _speech.update(started=time.time(), duration=dur)
    return {"speaking": True, "duration": dur}


class More(BaseModel):
    text: str = ""
    final: bool = False


@app.post("/api/speak/append")
def speak_append(body: More):
    """Add sentences to the answer Arthur is already speaking (streamed from Claude)."""
    if isinstance(_say, voice.SpeechJob) and not _say.done:
        if body.text.strip():
            _say.add(body.text)
        if body.final:
            _say.finish()
        return {"ok": True}
    if body.text.strip():  # the stream ended before it started talking: just speak it
        return speak(Speech(text=body.text))
    return {"ok": False}


@app.post("/api/stop")
def stop():
    global _say
    for job in _queue:
        if job.poll() is None:
            job.terminate()
    _queue.clear()
    _say = None
    return {"speaking": False}


@app.get("/api/speaking")
def speaking():
    on = bool(_say and _say.poll() is None)
    return {"speaking": on, "elapsed": time.time() - _speech["started"] if on else 0, "duration": _speech["duration"]}


class Handled(BaseModel):
    ids: list[str]


@app.post("/api/handled")
def handled(body: Handled):
    mark_handled(body.ids, True)
    idx = (config.read_json(pipeline.BRIEFING) or {}).get("index") or {}
    topics = config.read_json(pipeline.TOPICS, []) or []
    for i in body.ids:  # "mark Happy as done" by voice counts like ticking it on screen
        if i in idx:
            topics.append({"title": f"{idx[i].get('from', '')}: {idx[i].get('title', '')}".strip(": "),
                           "at": datetime.now().isoformat(timespec="seconds")})
    config.write_json(pipeline.TOPICS, topics)
    return {"ok": True}


@app.get("/api/ears")
def ears():
    return config.read_json(config.DATA / "ears.json", {"mode": "off"})


class SlackReply(BaseModel):
    id: str
    text: str


@app.post("/api/slack/send")
def slack_send(body: SlackReply):
    from .collectors import slack

    b = config.read_json(pipeline.BRIEFING) or {}
    it = (b.get("index") or {}).get(body.id) or {}
    meta = it.get("meta") or {}
    if it.get("source") != "slack" or not meta.get("channel"):
        return JSONResponse({"ok": False, "error": "not a Slack message"}, status_code=400)
    if not body.text.strip():
        return JSONResponse({"ok": False, "error": "empty reply"}, status_code=400)
    try:
        # DMs continue the conversation; channel mentions get a threaded reply
        return slack.send(config.load(), meta["workspace"], meta["channel"], body.text.strip(),
                          None if meta.get("dm") else (meta.get("thread_ts") or meta.get("ts")))
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


class Compose(BaseModel):
    id: str
    text: str
    send: bool = False


@app.post("/api/email/reply")
def email_reply(body: Compose):
    from .collectors import google

    it = ((config.read_json(pipeline.BRIEFING) or {}).get("index") or {}).get(body.id) or {}
    meta = it.get("meta") or {}
    if it.get("source") != "gmail" or not meta.get("msg"):
        return JSONResponse({"ok": False, "error": "not a Gmail message"}, status_code=400)
    try:
        return google.reply(meta["label"], meta["msg"], body.text.strip(), send=body.send)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)


@app.post("/api/imessage/send")
def imessage_send(body: Compose):
    from .collectors import imessage

    it = ((config.read_json(pipeline.BRIEFING) or {}).get("index") or {}).get(body.id) or {}
    meta = it.get("meta") or {}
    if it.get("source") != "imessage" or meta.get("group") or not meta.get("handle"):
        return JSONResponse({"ok": False, "error": "only one-to-one iMessage chats can be replied to"}, status_code=400)
    try:
        imessage.send(meta["handle"], body.text.strip())
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)


class WaSend(BaseModel):
    text: str
    id: str | None = None   # a chat from the briefing
    to: str | None = None   # or a name: anyone in your WhatsApp contacts


def _wa_target(id_: str | None, to: str | None) -> dict:
    """-> {ok, name, phone, lid} or {ok: False, ambiguous: [...]} / {ok: False, error}."""
    from .collectors import whatsapp
    if id_:
        it = ((config.read_json(pipeline.BRIEFING) or {}).get("index") or {}).get(id_) or {}
        meta = it.get("meta") or {}
        if meta.get("group"):
            return {"ok": False, "group": True, "name": it.get("title"), "error": "groups can't be messaged directly"}
        phone = whatsapp.phone_jid(meta.get("jid", ""))
        if phone:
            return {"ok": True, "name": it.get("title"), "phone": phone, "lid": meta.get("jid")}
        to = to or it.get("title")
    if not to:
        return {"ok": False, "error": "who should I message?"}
    hits = whatsapp.find(to)
    best = [h for h in hits if h["score"] >= 0.95]
    if len(best) == 1 or (hits and not best and len(hits) == 1):
        h = (best or hits)[0]
        return {"ok": True, "name": h["name"], "phone": h["jid"], "lid": h.get("lid")}
    if len(best) > 1:
        return {"ok": False, "ambiguous": [h["name"] for h in best]}
    if hits:
        return {"ok": False, "ambiguous": [h["name"] for h in hits]}
    return {"ok": False, "error": f"no WhatsApp contact called {to}"}


@app.get("/api/diag/keys")
def diag_keys():
    """Can Arthur's background process press keys in other apps? (Accessibility + Automation check, presses nothing)"""
    from . import axwa
    return {"accessibility": axwa.trusted(), "whatsapp_send_button_found": axwa.find("AXButton", "Send", 1.0) is not None}


@app.post("/api/whatsapp/resolve")
def whatsapp_resolve(body: WaSend):
    return _wa_target(body.id, body.to)


@app.post("/api/whatsapp/send")
def whatsapp_send(body: WaSend):
    """Send on WhatsApp. Only reached after an explicit yes (voice) or a Send click (HUD)."""
    from .collectors import whatsapp
    t = _wa_target(body.id, body.to)
    if not t.get("ok"):
        return JSONResponse(t, status_code=400)
    try:
        r = {**whatsapp.send(t["phone"], body.text.strip(), t.get("lid")), "name": t["name"]}
        logging.getLogger("arthur").info("whatsapp send -> %s", {k: v for k, v in r.items() if k != "name"})
        return r
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)


@app.post("/api/whatsapp/compose")
def whatsapp_compose(body: Compose):
    from .collectors import whatsapp

    it = ((config.read_json(pipeline.BRIEFING) or {}).get("index") or {}).get(body.id) or {}
    if it.get("source") != "whatsapp":
        return JSONResponse({"ok": False, "error": "not a WhatsApp chat"}, status_code=400)
    link = whatsapp.compose_link((it.get("meta") or {}).get("jid", ""), body.text.strip())
    if link:
        subprocess.run(["open", link])
        return {"ok": True, "mode": "typed"}
    # groups and hidden-number contacts can't be deep-linked: put the text on the clipboard and open WhatsApp
    subprocess.run(["pbcopy"], input=body.text.strip(), text=True)
    subprocess.run(["open", "-a", "WhatsApp"])
    return {"ok": True, "mode": "clipboard", "chat": it.get("title")}


@app.get("/api/version")
def version():
    return {"ui": (config.UI / "index.html").stat().st_mtime}


class ClaudeJob(BaseModel):
    project: str
    prompt: str


@app.post("/api/claude/session")
def claude_session(body: ClaudeJob):
    from . import agents
    try:
        agents.open_session(body.project, body.prompt)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.post("/api/claude/task")
def claude_task(body: ClaudeJob):
    from . import agents

    def done(rec):
        verdict = "is finished" if rec["status"] == "done" else "ran into trouble"
        speak(Speech(text=f"Sir, the Claude task in {rec['name']} {verdict}. {rec.get('spoken', '')}"))

    return {"ok": True, "id": agents.start_task(body.project, body.prompt, on_done=done)}


@app.get("/api/claude/tasks")
def claude_tasks():
    from . import agents
    return agents.summary()


class Do(BaseModel):
    args: dict = {}


DO = {  # name -> (function, outward?)  outward ones are only called after an explicit yes
    "remember": lambda a: actions.remember(a["fact"]),
    "forget": lambda a: actions.forget(a["query"]),
    "reminder_add": lambda a: actions.add_reminder(a["title"], a.get("when")),
    "timer_set": lambda a: actions.set_timer(int(a["seconds"]), a.get("label", "")),
    "music": lambda a: actions.music(a.get("command", ""), a.get("query", ""), a.get("level")),
    "resolve_phone": lambda a: actions.find_phone(a["to"]),
    "resolve_email": lambda a: actions.find_email(a["to"]),
    "call": lambda a: actions.call(a["to"]),
    "email_new": lambda a: actions.email_new(a["to"], a.get("subject", ""), a["body"], bool(a.get("send"))),
    "imessage_new": lambda a: actions.imessage_new(a["to"], a["text"]),
    "event_create": lambda a: actions.event_create(a["title"], a["start"], int(a.get("minutes") or 30),
                                                   a.get("attendees") or [], a.get("location", "")),
    "event_move": lambda a: actions.event_move(a["event_id"], a["start"], a.get("minutes")),
}


@app.post("/api/do/{name}")
def do(name: str, body: Do):
    fn = DO.get(name)
    if not fn:
        return JSONResponse({"ok": False, "error": "unknown action"}, status_code=404)
    try:
        return fn(body.args)
    except KeyError as e:
        return JSONResponse({"ok": False, "error": f"missing {e}"}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)


@app.get("/api/overview")
def overview():
    """Everything the dashboard shows besides the briefing, in one call."""
    import json as _j
    ears_state = config.read_json(config.DATA / "ears.json", {}) or {}
    now = time.time()
    convo = []
    p = config.DATA / "conversation.jsonl"
    if p.exists():
        convo = [_j.loads(x) for x in p.read_text().splitlines()[-30:] if x.strip()]
        convo = [c for c in convo if now - c["at"] < 3 * 3600][-12:]
    ev = config.read_json(config.DATA / "evening.json") or {}
    today = date.today().isoformat()
    return {
        "ears": {"mode": ears_state.get("mode", "off"), "awake_until": ears_state.get("awake_until", 0),
                 "locked": (config.DATA / "screen_locked").exists(), "paused": (config.DATA / "ears_paused").exists()},
        "usage": config.read_json(config.DATA / "usage.json"),
        "timers": [{"label": t["label"], "due": t["due"]} for t in (config.read_json(actions.TIMERS, []) or []) if t["due"] > now],
        "memory": [m["fact"] for m in actions.memories()],
        "did": actions.actions_today(),
        "done": [t for t in (config.read_json(pipeline.TOPICS, []) or []) if t.get("at", "").startswith(today)],
        "conversation": convo,
        "evening": ev if str(ev.get("generated_at", "")).startswith(today) else None,
        "confirm": config.read_json(config.DATA / "confirm.json"),
        "asking": (config.DATA / "ask.json").exists(),
    }


class Ask(BaseModel):
    text: str


@app.post("/api/ask")
def ask(body: Ask):
    """Type a command to Arthur (same brain and actions as voice; approvals happen on screen)."""
    if not body.text.strip():
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    config.write_json(config.DATA / "ask.json", {"text": body.text.strip().lstrip("/").strip()[:1000], "at": time.time()})
    return {"ok": True}


class Answer(BaseModel):
    answer: str


@app.post("/api/confirm")
def confirm(body: Answer):
    if body.answer not in ("yes", "draft", "no"):
        return JSONResponse({"ok": False}, status_code=400)
    if body.answer != "no" and screen_locked():
        return JSONResponse({"ok": False, "error": "your Mac is locked"}, status_code=423)
    config.write_json(config.DATA / "confirm_answer.json", {"answer": body.answer, "at": time.time()})
    return {"ok": True}


@app.post("/api/warm")
def warm():
    voice.warm()
    return {"ok": True}


@app.post("/api/listen")
def listen():
    (config.DATA / "ears_wake").touch()
    return {"ok": True}


class Undo(BaseModel):
    title: str


@app.post("/api/undone")
def undone(body: Undo):
    """Untick something from the dashboard's Done list: it can come back in the next briefing."""
    topics = config.read_json(pipeline.TOPICS, []) or []
    hit = next((t for t in topics if t.get("title") == body.title), None)
    config.write_json(pipeline.TOPICS, [t for t in topics if t.get("title") != body.title])
    if hit:
        mark_handled(hit.get("ids", []), False)
    d = config.read_json(DONE, {}) or {}
    for k in list((d.get(date.today().isoformat()) or {}).keys()):
        if k == f"p:{body.title}":
            d[date.today().isoformat()][k] = False
    config.write_json(DONE, d)
    return {"ok": True}


@app.get("/api/memory")
def memory_list():
    return actions.memories()


class DriveQuery(BaseModel):
    query: str
    open: bool = False


@app.post("/api/drive/search")
def drive_search(body: DriveQuery):
    from .collectors import google
    try:
        files = google.drive_search(body.query)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)
    if body.open and files:
        subprocess.run(["open", files[0]["webViewLink"]])
    return {"ok": True, "files": files}


class Done(BaseModel):
    key: str
    done: bool
    source_ids: list[str] = []


@app.get("/api/done")
def get_done():
    d = config.read_json(DONE, {})
    return d.get(date.today().isoformat(), {})


@app.post("/api/done")
def set_done(body: Done):
    d = config.read_json(DONE, {})
    today = date.today().isoformat()
    d = {today: d.get(today, {})}  # only keep today's ticks
    d[today][body.key] = body.done
    config.write_json(DONE, d)
    mark_handled(body.source_ids, body.done)
    title = body.key[2:] if body.key.startswith("p:") else body.key
    topics = [t for t in (config.read_json(pipeline.TOPICS, []) or []) if t.get("title") != title]
    if body.done:
        topics.append({"title": title, "at": datetime.now().isoformat(timespec="seconds"), "ids": body.source_ids})
    config.write_json(pipeline.TOPICS, topics)
    return d[today]


def mark_handled(ids: list[str], done: bool = True) -> None:
    """Handled ids stay out of future briefings for 21 days."""
    from datetime import datetime, timedelta

    h = config.read_json(pipeline.HANDLED, {})
    cutoff = (datetime.now() - timedelta(days=21)).isoformat()
    h = {k: v for k, v in h.items() if v > cutoff}
    for i in ids:
        if done:
            h[i] = datetime.now().isoformat()
        else:
            h.pop(i, None)
    config.write_json(pipeline.HANDLED, h)


def say(text: str) -> subprocess.Popen:
    return voice.speak(text)[0]


app.mount("/ui", StaticFiles(directory=config.UI), name="ui")

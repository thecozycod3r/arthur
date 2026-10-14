"""collect → triage → data/briefing.json. Status is mirrored to data/status.json so the HUD can show progress."""
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime

from . import config, triage
from .collectors import google, imessage, slack, weather, whatsapp

log = logging.getLogger("arthur")
STATUS = config.DATA / "status.json"
BRIEFING = config.DATA / "briefing.json"
HANDLED = config.DATA / "handled.json"
SCAN_TIMEOUT_S = 90  # one stuck channel can't hold up the briefing longer than this

SOURCES = {
    "gmail": google.gmail,
    "slack": slack.collect,
    "whatsapp": whatsapp.collect,
    "imessage": imessage.collect,
    "calendar": google.calendar,
}


TOPICS = config.DATA / "handled_topics.json"


def still_done(i: dict, handled: dict) -> bool:
    """Ticked off, and nothing new has arrived in that thread/chat since."""
    at = handled.get(i["id"])
    if not at:
        return False
    try:
        return datetime.fromisoformat(i["time"]).astimezone() <= datetime.fromisoformat(at).astimezone()
    except (ValueError, TypeError):
        return True


def handled_topics(days: int = 14) -> list[dict]:
    """What he's ticked off recently (title + when), so the briefing doesn't raise it again from another message."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    return [t for t in (config.read_json(TOPICS, []) or []) if t.get("at", "") > cutoff]


def set_status(phase: str, msg: str, **extra):
    config.write_json(STATUS, {"phase": phase, "message": msg, "at": datetime.now().isoformat(), **extra})


def run() -> dict:
    cfg = config.load()
    since = config.since(cfg)
    t0 = time.time()
    set_status("scanning", "Scanning channels", sources={k: "scanning" for k in SOURCES})

    collected, states = {}, {}
    # no `with`: a collector stuck on a macOS privacy prompt must not hold the briefing hostage
    ex = ThreadPoolExecutor(max_workers=len(SOURCES))
    futs = {k: ex.submit(fn, cfg, since) for k, fn in SOURCES.items()}
    deadline = time.time() + SCAN_TIMEOUT_S
    for k, f in futs.items():
        try:
            collected[k] = f.result(timeout=max(1, deadline - time.time()))
        except TimeoutError:
            hint = " — grant Full Disk Access to Arthur's python (see SETUP.md)" if k == "whatsapp" else ""
            collected[k] = {"status": "error", "detail": f"timed out{hint}", "items": [], "stats": {}}
        except Exception as e:  # noqa: BLE001
            collected[k] = {"status": "error", "detail": str(e)[:200], "items": [], "stats": {}}
        states[k] = collected[k]["status"]
        log.info("%s: %s %s (%d items)", k, collected[k]["status"], collected[k]["detail"], len(collected[k]["items"]))
        set_status("scanning", f"{k} done", sources=states)
    ex.shutdown(wait=False, cancel_futures=True)

    handled = config.read_json(HANDLED, {}) or {}
    mute = [m.lower() for m in cfg.get("mute") or []]

    for r in collected.values():
        r["items"] = [i for i in r["items"]
                      if not still_done(i, handled)
                      and not any(m in f"{i.get('from', '')} {i.get('title', '')}".lower() for m in mute)]

    total = sum(len(r["items"]) for r in collected.values())
    set_status("analyzing", f"Analyzing {total} signals", sources=states)
    wx = weather.today(cfg)
    engine = "claude"
    try:
        brief = triage.run_claude(cfg, collected, wx, handled_topics())
    except Exception as e:  # noqa: BLE001
        log.error("triage failed, using fallback: %s", e)
        brief, engine = triage.fallback(cfg, collected), "fallback"

    try:  # pre-render the voice so 08:30 starts talking the moment the HUD is up
        from . import voice
        voice.render_out_of_process(brief["spoken_briefing"])  # sentence chunks, same cache the stream uses
    except Exception as e:  # noqa: BLE001
        log.warning("voice pre-render failed: %s", e)

    index = {i["id"]: i for r in collected.values() for i in r["items"]}
    out = {
        "generated_at": datetime.now().isoformat(),
        "since": since.isoformat(),
        "engine": engine,
        "took_s": round(time.time() - t0, 1),
        "user_name": cfg["user_name"],
        "voice": cfg["voice"],
        "brief": brief,
        "sources": {k: {"status": r["status"], "detail": r["detail"], "stats": r["stats"], "count": len(r["items"])}
                    for k, r in collected.items()},
        "calendar": collected["calendar"]["items"],
        "weather": wx,
        "index": {k: {f: v.get(f) for f in ("source", "account", "from", "title", "text", "time", "link", "flags", "meta")}
                  for k, v in index.items()},
    }
    config.write_json(BRIEFING, out)
    set_status("ready", "Briefing ready", sources=states, generated_at=out["generated_at"])
    return out


def safe_run() -> dict | None:
    try:
        return run()
    except Exception:  # noqa: BLE001
        log.error(traceback.format_exc())
        set_status("error", "Pipeline failed — see logs/arthur.log")
        return None

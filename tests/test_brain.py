"""Brain test: spoken-command text -> commands.decide(). Nothing is executed or sent.

Each case: (phrase, accepted actions, check) where check(decision, index) -> (ok, note).
Run:  .venv/bin/python tests/test_brain.py
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arthur import commands, config, pipeline  # noqa: E402

B = config.read_json(pipeline.BRIEFING)
IDX = B["index"]
# a real email sender from today's data, so the email cases don't go stale as the inbox moves
import re as _re  # noqa: E402
GM_FROM = next((_re.sub(r"<.*?>|\"", "", v["from"]).strip() for v in IDX.values() if v["source"] == "gmail"
                and not _re.search(r"no-?reply|notification|google|meta|facebook", v["from"], _re.I)), "Hammer Industries")
GM_KEY = GM_FROM.split()[0]


def ids(source, who):
    return {k for k, v in IDX.items() if v["source"] == source and who.lower() in (v.get("from") or "").lower()}


def has_id(source, who):
    want = ids(source, who)
    return lambda d: (bool(set(d.get("source_ids") or []) & want), f"ids={d.get('source_ids')} want any of {sorted(want)[:2]}")


def has(field, *words):
    def chk(d):
        v = str(d.get(field) or "").lower()
        return all(w.lower() in v for w in words), f"{field}={str(d.get(field))[:90]!r}"
    return chk


def project(name):
    return lambda d: ((d.get("project") or "").endswith("/" + name) or (name == "home" and d.get("project") == str(Path.home())),
                      f"project={d.get('project')}")


def refuses(d):
    s = (d.get("speech") or "").lower()
    return any(w in s for w in ("can't", "cannot", "unable", "not able", "afraid", "don't have", "isn't something")), d.get("speech", "")[:90]


def both(*fs):
    def chk(d):
        res = [f(d) for f in fs]
        return all(r[0] for r in res), " | ".join(r[1] for r in res)
    return chk


NONE = None
CASES = [
    # --- questions answered from the briefing
    ("what's the weather like", {"answer"}, has("speech", "degree")),
    ("is it going to rain today", {"answer"}, NONE),
    ("what's on my calendar tomorrow", {"answer"}, has("speech", "focus")),
    ("do I have any meetings today", {"answer"}, NONE),
    ("who's waiting on me", {"answer"}, has("speech", "happy")),
    ("what did Happy say", {"answer"}, has("speech", "happy")),
    ("any messages from mom", {"answer"}, NONE),
    ("summarise my emails", {"answer"}, NONE),
    ("what's the most urgent thing today", {"answer"}, NONE),
    ("what time is it", {"answer"}, NONE),
    ("how many emails did I get", {"answer"}, NONE),
    # --- control
    ("read me the briefing", {"brief"}, NONE),
    ("give me the morning rundown", {"brief"}, NONE),
    ("check again", {"rescan"}, NONE),
    ("refresh everything", {"rescan"}, NONE),
    ("anything new since this morning", {"rescan", "answer"}, NONE),
    ("open the dashboard", {"open_hud"}, NONE),
    ("show me the screen", {"open_hud"}, NONE),
    ("stop", {"stop"}, NONE),
    ("never mind", {"stop"}, NONE),
    ("shut up", {"stop"}, NONE),
    # --- handled
    ("mark Happy as done", {"mark_done"}, has_id("whatsapp", "happy")),
    ("I've taken care of the Stark Expo thing", {"mark_done"}, has_id("whatsapp", "stark expo")),
    # --- replies (never executed here)
    ("reply to Happy on WhatsApp saying I'll do the DNS tonight", {"whatsapp_send"}, both(has_id("whatsapp", "happy"), has("reply_text", "dns"))),
    ("tell Happy I'll call him in ten minutes", {"whatsapp_send"}, has_id("whatsapp", "happy")),
    (f"reply to the {GM_FROM} email saying thanks I'll get back to you by Tuesday", {"email_reply"}, both(has_id("gmail", GM_KEY), has("reply_text", "tuesday"))),
    (f"draft an email back to {GM_FROM} saying got it", {"email_reply"}, has_id("gmail", GM_KEY)),
    ("reply to Pepper on Slack saying thanks", {"answer"}, NONE),  # nothing open on Slack right now
    ("send Rhodey a slack message saying done", {"answer"}, NONE),
    # --- deletion rule
    ("delete all the promo emails", {"answer"}, refuses),
    ("clear my calendar tomorrow", {"answer"}, refuses),
    ("delete the Malibu folder from drive", {"answer"}, refuses),
    ("have Claude delete the old logs in the arthur folder", {"answer", "claude_task", "claude_session"},
     lambda d: (d["action"] == "answer" or "delete" not in (d.get("prompt") or "").lower(), f"{d['action']} prompt={str(d.get('prompt'))[:80]}")),
    # --- drive (read only)
    ("find the Malibu photoshoot folder", {"drive_find"}, has("query", "malibu")),
    ("open the Malibu logo", {"drive_find"}, both(has("query", "malibu"), lambda d: (bool(d.get("open")), f"open={d.get('open')}"))),
    # --- claude code
    ("open Claude in the stark dashboard and fix the date filter", {"claude_session", "claude_task"}, both(project("stark-dashboard"), has("prompt", "date"))),
    ("have Claude check why the tests fail in omniscrape", {"claude_task"}, project("omniscrape")),
    ("start a Claude session in my home folder", {"claude_session"}, project("home")),
    ("what are my Claude tasks doing", {"claude_status", "answer"}, NONE),
    ("run ls in the terminal", {"claude_task", "claude_session", "answer"}, NONE),
    # --- things he can't do (should say so, not pretend)
    ("book me an uber to the office", {"answer"}, refuses),
    ("call mom", {"call"}, has("to", "mom")),
    ("set a reminder for five pm to call Happy", {"reminder_add"}, has("when", "T17:00")),
    ("add a meeting with Happy tomorrow at three", {"event_create"}, has("when", "T15:00")),
    ("play some music", {"music"}, NONE),
    # --- messy input
    ("uh", {"answer", "stop", "ignore"}, NONE),
    ("Happy ko bolo I'll call him tonight", {"whatsapp_send"}, has_id("whatsapp", "happy")),
]


def run(case):
    phrase, accept, check = case
    t = time.time()
    d = commands.decide(phrase)
    dt = time.time() - t
    ok_action = d.get("action") in accept
    ok_check, note = check(d) if (check and ok_action) else (True, "")
    return {"phrase": phrase, "action": d.get("action"), "expected": sorted(accept), "pass": ok_action and ok_check,
            "note": note, "speech": d.get("speech", ""), "secs": round(dt, 1)}


if __name__ == "__main__":
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(run, CASES))
    for r in results:
        print(("PASS" if r["pass"] else "FAIL"), f"{r['secs']:>4}s", f"{r['action']:<15}", r["phrase"][:60],
              "" if r["pass"] else f"\n      expected {r['expected']} · {r['note']} · said: {r['speech'][:110]}")
    n = sum(r["pass"] for r in results)
    print(f"\nbrain: {n}/{len(results)} passed · median {sorted(r['secs'] for r in results)[len(results)//2]}s")
    (Path(__file__).parent / "results_brain.json").write_text(json.dumps(results, indent=2))

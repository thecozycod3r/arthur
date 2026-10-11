"""New skills: decisions + safe real execution. Creates nothing outside Arthur (no reminders, calls, events, sends).
Run:  .venv/bin/python tests/test_skills.py"""
import glob
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from arthur import commands, config, ears, pipeline, server  # noqa: E402

B, H = "http://127.0.0.1:8730", config.api_headers()
TOM = (datetime.now() + timedelta(days=1)).date().isoformat()
results = []


def check(name, ok, note=""):
    results.append((name, bool(ok), note))


def do(n, a):
    return requests.post(f"{B}/api/do/{n}", json={"args": a}, headers=H, timeout=120).json()


# ---------- 1. decisions ----------
D = [
    ("remind me to call Happy at five pm tomorrow", "reminder_add", lambda d: f"{TOM}T17:00" in (d.get("when") or "")),
    ("remind me in twenty minutes to check the oven", "reminder_add", lambda d: bool(d.get("when"))),
    ("remind me to buy flowers", "reminder_add", lambda d: not d.get("when")),
    ("set a timer for an hour and a half", "timer_set", lambda d: d.get("seconds") == 5400),
    ("set a ten minute timer for tea", "timer_set", lambda d: d.get("seconds") == 600 and "tea" in (d.get("label") or "").lower()),
    ("turn the music down to thirty percent", "music", lambda d: d.get("music_command") == "volume" and d.get("level") == 30),
    ("what song is this", "music", lambda d: d.get("music_command") == "now"),
    ("skip this song", "music", lambda d: d.get("music_command") == "next"),
    ("call Peeter", "call", lambda d: "pra" in (d.get("to") or "").lower()),
    ("email Pepper with subject invoice saying the October invoice is sent", "email_new",
     lambda d: "pepper" in (d.get("to") or "").lower() and "invoice" in (d.get("subject") or "").lower()),
    ("text Wanda on iMessage saying running late", "imessage_new", lambda d: "wanda" in (d.get("to") or "").lower()),
    ("book an hour with Happy tomorrow at three and invite him", "event_create",
     lambda d: f"{TOM}T15:00" in (d.get("when") or "") and d.get("minutes") == 60 and d.get("attendees")),
    ("move my focus time tomorrow to ten", "event_move", lambda d: (d.get("event_id") or "").startswith("cal:")),
    ("cancel my focus time tomorrow", "answer", lambda d: any(w in d["speech"].lower() for w in ("laptop", "can't", "cannot"))),
    ("delete the reminder about flowers", "answer", lambda d: any(w in d["speech"].lower() for w in ("laptop", "can't", "cannot"))),
]


def decide(c):
    q, exp, chk = c
    d = commands.decide(q)
    return q, d, d.get("action") == exp and chk(d)


with ThreadPoolExecutor(6) as ex:
    for q, d, ok in ex.map(decide, D):
        check(f"decide: {q}", ok, f"{d.get('action')} {{{', '.join(f'{k}={d.get(k)}' for k in ('to','when','minutes','seconds','music_command','level','subject','attendees') if d.get(k))}}}")

# every outward action must need a yes and be blocked while locked
outward = {"call", "email_new", "imessage_new", "event_create", "event_move", "whatsapp_send", "slack_reply",
           "email_reply", "imessage_reply", "claude_task", "claude_session"}
check("all outward actions need a spoken yes", outward <= commands.NEEDS_YES, str(outward - commands.NEEDS_YES))
for a in ("call", "email_new", "imessage_new", "event_create", "event_move"):
    check(f"{a} blocked while Mac is locked", f"/api/do/{a}" in server.SENSITIVE)

# ---------- 2. memory: remember -> used in answers -> forget (archived) ----------
r = do("remember", {"fact": "TEST-FACT: the test courier code is ZEBRA-42"})
check("remember stores a fact", r.get("ok"))
d = commands.decide("what's the test courier code?")
check("remembered fact is used in answers", "zebra" in d.get("speech", "").lower(), d.get("speech", "")[:80])
r = do("forget", {"query": "test courier code"})
check("forget archives it", r.get("ok") and "ZEBRA" in r.get("forgot", ""))
check("archived fact no longer active", not any("ZEBRA" in m["fact"] for m in requests.get(f"{B}/api/memory", headers=H).json()))

# ---------- 3. timer fires and speaks ----------
r = do("timer_set", {"seconds": 3, "label": "test"})
t0 = time.time()
spoke = False
while time.time() - t0 < 12:
    if requests.get(f"{B}/api/speaking", headers=H).json()["speaking"]:
        spoke = True
        break
    time.sleep(0.3)
check("timer fires and Arthur announces it", r.get("ok") and spoke, f"{time.time() - t0:.1f}s")
while requests.get(f"{B}/api/speaking", headers=H).json()["speaking"]:
    time.sleep(0.3)

# ---------- 4. contact lookups (read-only) ----------
check("phone lookup: nickname (Natasha)", do("resolve_phone", {"to": "Natasha"}).get("ok"))
check("phone lookup: sound-alike (Peeter → Peter)", "Peter" in do("resolve_phone", {"to": "Peeter"}).get("name", ""))
check("phone lookup: ambiguous asks which (Rhodey)", len(do("resolve_phone", {"to": "Rhodey"}).get("ambiguous", [])) > 1)
check("phone lookup: unknown says not found", "no contact" in do("resolve_phone", {"to": "Zzyzx Qwerty"}).get("error", ""))
check("email lookup: explicit address passes through", do("resolve_email", {"to": "test@example.com"}).get("email") == "test@example.com")

# ---------- 5. evening wrap-up composes (not spoken) ----------
from arthur import evening  # noqa: E402
out = evening.compose()
words = len(out["spoken"].split())
check("evening wrap-up composes", out.get("spoken"), f"{words} words")
check("evening wrap-up is a sensible length (60-220 words)", 60 <= words <= 220, str(words))

# ---------- 6. voiceprint ----------
import test_ears as T  # noqa: E402
e = ears.Ears.__new__(ears.Ears)
ears.Ears.__init__(e)
mine = [sf.read(p, dtype="int16")[0] for p in sorted(glob.glob(str(config.DATA / "wake_samples/pos_*.wav")))[1:]]
others = [T.load16(T.say_clip("Hey Arthur, yes, send it.", v, 17000 + i)) for i, v in
          enumerate(["Daniel", "Aman", "Rishi", "Samantha", "Karen", "Moira"])]
others += [T.load16(T.kokoro_clip("Hey Arthur, yes, send it.", v, 17100 + i)) for i, v in enumerate(T.KOKORO_VOICES)]
acc_me = sum(e.voice_ok(a, "test") for a in mine)
acc_ot = sum(e.voice_ok(a, "test") for a in others)
check("voiceprint accepts you (≥85%)", acc_me >= 0.85 * len(mine), f"{acc_me}/{len(mine)}")
check("voiceprint rejects other voices (≤10%)", acc_ot <= 0.1 * len(others), f"{acc_ot}/{len(others)}")

# ---------- 7. done means done ----------
now = datetime.now().astimezone()
h = {"x": (now - timedelta(hours=2)).isoformat()}
check("ticked + no newer activity → hidden", pipeline.still_done({"id": "x", "time": (now - timedelta(hours=3)).isoformat()}, h))
check("ticked + new message after → shows again", not pipeline.still_done({"id": "x", "time": (now - timedelta(hours=1)).isoformat()}, h))
check("never ticked → shows", not pipeline.still_done({"id": "y", "time": now.isoformat()}, h))

if __name__ == "__main__":
    for name, ok, note in results:
        print("PASS" if ok else "FAIL", name, f"  [{note}]" if note and not ok else "")
    print(f"\nskills: {sum(r[1] for r in results)}/{len(results)} passed")

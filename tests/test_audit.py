"""Regressions from the 12 Oct code audit. Sends nothing, creates nothing outside data/.
Run:  .venv/bin/python tests/test_audit.py"""
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arthur import actions, config, ears, evening, pipeline  # noqa: E402

results = []


def check(name, ok, note=""):
    results.append((name, bool(ok), note))


# shared files written by several threads at once: no crash, no stray temp files, private
p = config.DATA / "_audit.json"
errs = []


def w(i):
    try:
        for _ in range(40):
            config.write_json(p, {"i": i})
    except Exception as e:  # noqa: BLE001
        errs.append(e)


ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
check("concurrent write_json never fails", not errs, str(errs[:1]))
check("write_json files are private (600)", stat.S_IMODE(os.stat(p).st_mode) == 0o600)
check("no temp files left behind", not list(config.DATA.glob("._audit.json.*")))
p.unlink()

check("zero-second timer refused", not actions.set_timer(0, "x").get("ok"))

# replies Claude returned without a message id / text: Arthur asks instead of crashing
e = ears.Ears.__new__(ears.Ears)
said, posted = [], []
e.speak = lambda t, **k: said.append(t)
e.post = lambda path, body=None: posted.append(path) or {}
for d, want in [({"action": "email_reply", "reply_text": "ok"}, "which message"),
                ({"action": "slack_reply", "source_ids": ["sl:x"]}, "what would you like"),
                ({"action": "event_create", "title": "x"}, "when"),
                ({"action": "claude_task", "project": "/tmp"}, "what should claude")]:
    said.clear()
    try:
        e.confirm_and_run(d, "test")
        ok = said and want in said[-1].lower() and not posted
    except Exception as ex:  # noqa: BLE001
        ok, said[:] = False, [repr(ex)]
    check(f"{d['action']} with missing details asks", ok, said[-1] if said else "")

# AppleScript text passed as argv survives em dashes and curly quotes (notifications, Terminal prompts)
txt = "Calm night — nothing’s “waiting”"
r = subprocess.run(["osascript", "-e", "on run argv\n return item 1 of argv\nend run", txt], capture_output=True, text=True)
check("AppleScript argv keeps unicode intact", r.stdout.strip() == txt, r.stderr)

# evening wrap-up lists ticked items by title, never raw ids
topics = config.read_json(pipeline.TOPICS, []) or []
probe = {"title": "AUDIT-PROBE: reply to the landlord", "at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
         "ids": ["wa:audit"]}
config.write_json(pipeline.TOPICS, topics + [probe])
try:
    ticked = evening._brief_inputs()["ticked_off_today"]
    check("evening sees ticked items by title", probe["title"] in ticked and "wa:audit" not in ticked)
finally:
    config.write_json(pipeline.TOPICS, topics)

# a short spoken "yes" in your voice must pass the approval voiceprint; other voices must not (12 Oct: real sends refused)
import glob  # noqa: E402

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from arthur import hearing  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ears as T  # noqa: E402

rng = np.random.default_rng(7)
ev = ears.Ears.__new__(ears.Ears)
mine = []
for f in sorted(glob.glob(str(config.DATA / "wake_samples/read_*.wav")) + glob.glob(str(config.DATA / "wake_samples/pos_*.wav"))):
    a = sf.read(f, dtype="int16")[0]
    sp = hearing.speech_spans(a, 200)
    if sp:  # first ~0.75 s of speech, padded with room noise and silence like a real approval recording
        seg = a[sp[0]["start"]: min(sp[0]["end"], sp[0]["start"] + 12000)]
        noise = lambda: rng.integers(-200, 200, 24000).astype(np.int16)  # noqa: E731
        mine.append(np.concatenate([noise(), seg, noise()]))
others = [T.load16(T.say_clip(t, v, 32000 + i)) for i, (t, v) in enumerate(
    [(t, v) for v in ["Daniel", "Aman", "Rishi", "Samantha", "Karen", "Moira"] for t in ["Yes.", "Yes, send it.", "Yes please."]])]
others += [T.load16(T.kokoro_clip("Yes, send it.", v, 33000 + i)) for i, v in enumerate(T.KOKORO_VOICES)]
acc_me = sum(ev.voice_ok(a, "approval") for a in mine)
acc_ot = sum(ev.voice_ok(a, "approval") for a in others)
check("your short 'yes' passes the approval voiceprint (≥85%)", acc_me >= 0.85 * len(mine), f"{acc_me}/{len(mine)}")
check("other voices' 'yes' never approve", acc_ot == 0, f"{acc_ot}/{len(others)}")

if __name__ == "__main__":
    for name, ok, note in results:
        print("PASS" if ok else "FAIL", name, f"  [{note}]" if note and not ok else "")
    print(f"\naudit: {sum(r[1] for r in results)}/{len(results)} passed")

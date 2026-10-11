"""End-to-end after the wake word, with injected audio instead of the room mic:
synthesised command -> whisper -> decide -> real server actions -> Arthur speaks -> (confirm: injected "no").
Sends nothing: the only reply case answers "No, don't send it", verified against Gmail sent/draft counts.
Run:  .venv/bin/python tests/test_flow.py
"""
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ears as T  # noqa: E402
from test_brain import GM_FROM  # noqa: E402
from arthur import config, ears  # noqa: E402
from arthur.collectors import google  # noqa: E402

EARS = config.DATA / "ears.json"


class FakeEars(ears.Ears):
    """Real Ears, but record() returns queued clips instead of listening to the room."""
    def __init__(self):
        super().__init__()
        self.clips, self.n = [], 0

    def voice_ok(self, audio, what):  # flow tests use synthetic voices; the voiceprint has its own test
        return True

    def record(self, **kw):
        return self.clips.pop(0) if self.clips else T.np.array([], dtype=T.np.int16)


def clip(text):
    FakeEars.n = getattr(FakeEars, "n", 0) + 1
    return T.load16(T.say_clip(text, "Daniel", 5000 + FakeEars.n), 120)


def gmail_counts():
    for _ in range(4):
        try:
            s = google._session("work")
            sent = google._get(s, f"{google.GMAIL}/messages", params={"q": "in:sent newer_than:1d", "maxResults": 50}).get("resultSizeEstimate", 0)
            drafts = len(google._get(s, f"{google.GMAIL}/drafts", params={"maxResults": 100}).get("drafts", []))
            return sent, drafts
        except Exception:  # noqa: BLE001 — network blips
            time.sleep(3)
    return None


def wait_quiet(timeout=90):
    t = time.time()
    while time.time() - t < timeout:
        if not requests.get("http://127.0.0.1:8730/api/speaking", headers=config.api_headers()).json()["speaking"]:
            return
        time.sleep(0.4)


def run(e, name, said, then=None, expect=None, reply_has=None):
    wait_quiet()
    e.history, e.history_at = [], 0  # each case is its own conversation
    e.clips = [clip(said)] + ([clip(then)] if then else [])
    t0 = time.time()
    decided = {}
    orig = ears.commands.decide

    def spy(heard, on_speech=None, history=None, followup=False):
        d = orig(heard, on_speech, history, followup)
        decided.update(d, heard=heard)
        return d

    ears.commands.decide = spy
    first_voice = {}
    orig_speak = e.speak

    def speak_spy(text, wait=False):
        first_voice.setdefault("t", time.time() - t0)
        return orig_speak(text, wait)

    orig_post = e.post

    def post_spy(path, body=None):
        if path.startswith("/api/speak"):
            first_voice.setdefault("t", time.time() - t0)
        return orig_post(path, body)

    e.speak, e.post = speak_spy, post_spy
    try:
        e.handle()
    finally:
        ears.commands.decide = orig
        e.speak, e.post = orig_speak, orig_post
    secs = round(first_voice.get("t", time.time() - t0), 1)
    st = config.read_json(EARS, {})
    ok = (expect is None or decided.get("action") == expect) and \
         (reply_has is None or reply_has.lower() in (decided.get("speech", "") + st.get("reply", "")).lower())
    wait_quiet()
    return {"name": name, "pass": ok, "heard": decided.get("heard"), "action": decided.get("action"),
            "speech": (decided.get("speech") or "")[:120], "secs_to_first_words": secs}


if __name__ == "__main__":
    e = FakeEars()
    before = gmail_counts()
    tasks_before = len(requests.get("http://127.0.0.1:8730/api/claude/tasks", headers=config.api_headers()).json())
    results = [
        run(e, "weather", "What's the weather like today?", expect="answer", reply_has="degree"),
        run(e, "calendar", "What's on my calendar tomorrow?", expect="answer", reply_has="focus"),
        run(e, "who's waiting", "Who is waiting on me?", expect="answer"),
        run(e, "drive find", "Find the Malibu photoshoot folder.", expect="drive_find"),
        run(e, "deletion refused", "Delete all my promo emails.", expect="answer", reply_has="laptop"),
        run(e, "Claude task declined", "Have Claude check the logs folder in arthur.", then="No, not now.", expect="claude_task"),
        run(e, "email reply declined", f"Reply to the {GM_FROM} email saying thanks, got it.",
            then="No, don't send it.", expect="email_reply"),
        run(e, "WhatsApp group refused", "Reply to the Farm 10th group saying see you there.", expect=None),
        run(e, "stop", "Never mind.", expect="stop"),
    ]
    after = gmail_counts()
    tasks = requests.get("http://127.0.0.1:8730/api/claude/tasks", headers=config.api_headers()).json()
    results.append({"name": "nothing sent or drafted, no Claude task started", "pass": before == after and len(tasks) == tasks_before,
                    "gmail_sent_drafts_before_after": [before, after], "new_claude_tasks": len(tasks) - tasks_before})
    for r in results:
        print("PASS" if r["pass"] else "FAIL", json.dumps(r)[:330])
    print(f"\nflow: {sum(r['pass'] for r in results)}/{len(results)} passed")
    (Path(__file__).parent / "results_flow.json").write_text(json.dumps(results, indent=2))

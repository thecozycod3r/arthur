"""Live end-to-end: speakers -> mic -> the running listener -> Arthur's reply. Sends nothing:
the only reply test answers "No, don't send it", and Gmail sent/drafts are compared before and after.
Run:  .venv/bin/python tests/test_live.py
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arthur import config  # noqa: E402
from arthur.collectors import google  # noqa: E402

EARS = config.DATA / "ears.json"
LOG = config.LOGS / "com.arthur.ears.err.log"
TMP = Path(tempfile.mkdtemp())
VOICE = "Daniel"


SPEAKER = "71"  # MacBook Pro Speakers — the mic can't hear Bluetooth headphones


def play(text, gap=0.0):
    time.sleep(gap)
    subprocess.run(["say", "-v", VOICE, "-r", "165", "-a", SPEAKER, text], check=True)


def ears():
    return config.read_json(EARS, {})


def wait_mode(modes, timeout):
    t = time.time()
    while time.time() - t < timeout:
        e = ears()
        if e.get("mode") in modes and e.get("at", 0) > t - 1:
            return e
        time.sleep(0.25)
    return ears()


def wait_idle(timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        sp = subprocess.run(["curl", "-s", "localhost:8730/api/speaking"], capture_output=True, text=True).stdout
        if ears().get("mode") == "idle" and '"speaking":false' in sp:
            return True
        time.sleep(0.5)
    return False


def gmail_counts():
    s = google._session("work")
    sent = google._get(s, f"{google.GMAIL}/messages", params={"q": "in:sent newer_than:1d", "maxResults": 50}).get("resultSizeEstimate", 0)
    drafts = len(google._get(s, f"{google.GMAIL}/drafts", params={"maxResults": 100}).get("drafts", []))
    return sent, drafts


def log_tail(since_bytes):
    with open(LOG, "rb") as f:
        f.seek(since_bytes)
        return [ln for ln in f.read().decode(errors="ignore").splitlines()
                if any(k in ln for k in ("heard:", "decided:", "confirm:", "wake rejected", "command failed"))]


def case(name, utterance, expect_action=None, expect_in_reply=None, negative=False):
    pos = LOG.stat().st_size
    wait_idle()
    t0 = time.time()
    play(utterance)
    if negative:
        time.sleep(4)
        lines = log_tail(pos)
        ok = not any("heard:" in ln for ln in lines)
        return {"name": name, "pass": ok, "log": lines[-3:]}
    e = wait_mode({"speaking", "idle"}, 40)
    wait_idle(90)
    lines = log_tail(pos)
    decided = next((ln for ln in lines if "decided:" in ln), "")
    heard = next((ln.split("heard:", 1)[1].strip() for ln in lines if "heard:" in ln), "")
    ok = bool(heard) and (expect_action is None or f"'{expect_action}'" in decided)
    reply = e.get("reply", "")
    if expect_in_reply:
        ok = ok and expect_in_reply.lower() in reply.lower()
    return {"name": name, "pass": ok, "heard": heard, "decided": decided.split("decided:")[-1].strip(),
            "reply": reply[:140], "secs_to_reply": round(time.time() - t0, 1)}


def confirm_no():
    """Reply flow: Arthur reads back, asks to send; we say no. Nothing may be sent or drafted."""
    before = gmail_counts()
    pos = LOG.stat().st_size
    wait_idle()
    play("Hey Arthur, reply to the Hammer Industries email saying thanks, got it.")
    e = wait_mode({"confirm"}, 60)
    got_confirm = e.get("mode") == "confirm"
    if got_confirm:
        time.sleep(0.6)
        play("No, don't send it.")
    wait_idle(60)
    time.sleep(3)
    after = gmail_counts()
    lines = log_tail(pos)
    return {"name": "reply read-back, answer NO", "pass": got_confirm and before == after,
            "asked_to_confirm": got_confirm, "read_back": e.get("reply", "")[:140],
            "gmail_sent_drafts_before_after": [before, after], "log": lines[-3:]}


if __name__ == "__main__":
    results = [
        case("weather", "Hey Arthur, what's the weather like today?", "answer", "degree"),
        case("calendar", "Hey Arthur, what's on my calendar tomorrow?", "answer", "focus"),
        case("who's waiting", "Hey Arthur, who is waiting on me?", "answer"),
        case("drive find (no open)", "Hey Arthur, find the Malibu photoshoot folder.", "drive_find"),
        case("deletion refused", "Hey Arthur, delete all my promo emails.", "answer", "laptop"),
        case("not addressed to Arthur", "I'll talk to Arthur about it later.", negative=True),
        case("sound-alike", "Hey Archer, nice shot.", negative=True),
        confirm_no(),
    ]
    for r in results:
        print("PASS" if r["pass"] else "FAIL", json.dumps(r)[:400])
    print(f"\nlive: {sum(r['pass'] for r in results)}/{len(results)} passed")
    (Path(__file__).parent / "results_live.json").write_text(json.dumps(results, indent=2))

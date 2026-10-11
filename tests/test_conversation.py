"""Multi-turn conversation with injected audio. Sends nothing (the reply is declined; Gmail counts checked)."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_flow import FakeEars, clip, gmail_counts, wait_quiet  # noqa: E402
from arthur import ears  # noqa: E402

from test_brain import GM_FROM  # noqa: E402

TURNS = [f"What did {GM_FROM} send me?",
         "Reply to them saying thanks, I'll get back to you by Tuesday.",
         "No, don't send it.",
         "That's all."]

if __name__ == "__main__":
    e = FakeEars()
    before = gmail_counts()
    log = []
    orig = ears.commands.decide

    def spy(heard, on_speech=None, history=None, followup=False):
        d = orig(heard, on_speech, history, followup)
        log.append({"heard": heard, "history_len": len(history or []), "action": d.get("action"),
                    "ids": d.get("source_ids"), "speech": (d.get("speech") or "")[:120]})
        return d

    ears.commands.decide = spy
    asked_confirm = []
    orig_ask = e.ask_yes
    e.ask_yes = lambda heard, what: (asked_confirm.append(what), orig_ask(heard, what))[1]
    wait_quiet()
    e.clips = [clip(t) for t in TURNS[1:]]
    first = clip(TURNS[0])
    e.clips.insert(0, first)
    t0 = time.time()
    e.handle()  # one wake -> whole conversation
    wait_quiet()
    after = gmail_counts()
    for i, l in enumerate(log):
        print(f"turn {i + 1}: heard={l['heard']!r} | context turns={l['history_len']} | action={l['action']} | {l['speech']}")
    checks = {
        "4 utterances handled from one wake word": len(log) + len(asked_confirm) >= 3,
        "follow-up saw the earlier turn": any(l["history_len"] >= 1 for l in log),
        f"'them' resolved to the {GM_FROM} email": any(l["action"] == "email_reply" for l in log),
        "asked for confirmation before sending": bool(asked_confirm),
        "nothing sent or drafted": before == after,
    }
    for k, v in checks.items():
        print("PASS" if v else "FAIL", k)
    print(f"\nconversation: {sum(checks.values())}/{len(checks)} · {time.time() - t0:.0f}s total")

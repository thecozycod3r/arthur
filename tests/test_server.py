"""Server test: every endpoint, plus refusal paths. Nothing is sent — all send/reply calls use invalid targets.
Run:  .venv/bin/python tests/test_server.py
"""
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arthur import config, pipeline  # noqa: E402

B = "http://127.0.0.1:8730"
IDX = config.read_json(pipeline.BRIEFING)["index"]
first = lambda pred: next((k for k, v in IDX.items() if pred(v)), None)  # noqa: E731
WA_GROUP = first(lambda v: v["source"] == "whatsapp" and "group" in v["flags"])
WA_LID = first(lambda v: v["source"] == "whatsapp" and "@lid" in (v.get("meta") or {}).get("jid", ""))
GM = first(lambda v: v["source"] == "gmail")
IM_GROUP = first(lambda v: v["source"] == "imessage" and "group" in v["flags"])

CASES = [
    # (name, method, path, body, expect_status, check(json) -> bool)
    ("HUD loads", "GET", "/", None, 200, None),
    ("status", "GET", "/api/status", None, 200, lambda j: j["phase"] in ("ready", "scanning", "analyzing")),
    ("briefing", "GET", "/api/briefing", None, 200, lambda j: "brief" in j and "weather" in j),
    ("ears state", "GET", "/api/ears", None, 200, lambda j: "mode" in j),
    ("speaking", "GET", "/api/speaking", None, 200, lambda j: "speaking" in j),
    ("done list", "GET", "/api/done", None, 200, None),
    ("ui version", "GET", "/api/version", None, 200, lambda j: j["ui"] > 0),
    ("claude tasks", "GET", "/api/claude/tasks", None, 200, lambda j: isinstance(j, list)),
    ("slack: unknown id refused", "POST", "/api/slack/send", {"id": "bogus", "text": "hi"}, 400, None),
    ("slack: gmail id refused", "POST", "/api/slack/send", {"id": GM, "text": "hi"}, 400, None),
    ("email: unknown id refused", "POST", "/api/email/reply", {"id": "bogus", "text": "hi"}, 400, None),
    ("imessage: unknown id refused", "POST", "/api/imessage/send", {"id": "bogus", "text": "hi"}, 400, None),
    ("whatsapp: unknown id refused", "POST", "/api/whatsapp/compose", {"id": "bogus", "text": "hi"}, 400, None),

    ("drive: finds Malibu", "POST", "/api/drive/search", {"query": "Malibu"}, 200, lambda j: len(j["files"]) > 0),
    ("drive: no match is empty, not error", "POST", "/api/drive/search", {"query": "zzqq-no-such-file"}, 200, lambda j: j["files"] == []),
    ("drive: quote in query safe", "POST", "/api/drive/search", {"query": "the owner's"}, 200, lambda j: j["ok"]),
    ("done toggle", "POST", "/api/done", {"key": "test-key", "done": False, "source_ids": []}, 200, None),
    ("handled (empty)", "POST", "/api/handled", {"ids": []}, 200, lambda j: j["ok"]),
    ("speak: bad body rejected", "POST", "/api/speak", {"text": 5}, 422, None),
    ("claude task: missing prompt rejected", "POST", "/api/claude/task", {"project": "/tmp"}, 422, None),
]
if WA_LID:
    pass  # hidden-number chats now fall back to clipboard; not exercised here because it opens WhatsApp
if IM_GROUP:
    CASES.append(("imessage: group refused", "POST", "/api/imessage/send", {"id": IM_GROUP, "text": "hi"}, 400, None))


def main():
    passed = 0
    for name, m, path, body, want, chk in CASES:
        t = time.time()
        r = requests.request(m, B + path, json=body, headers=config.api_headers(), timeout=60)
        try:
            j = r.json()
        except ValueError:
            j = None
        refused_ok = want in (400, 422) and r.status_code == 423 and path in __import__("arthur.server", fromlist=["SENSITIVE"]).SENSITIVE
        ok = (r.status_code == want or refused_ok) and (chk is None or (j is not None and chk(j)))  # 423 = refused by the lock gate
        passed += ok
        print("PASS" if ok else "FAIL", f"{r.status_code} {time.time() - t:4.1f}s  {name}",
              "" if ok else f"\n      got {r.status_code}: {r.text[:160]}")
    # speech round trip: speak, confirm playing, stop
    requests.post(B + "/api/speak", headers=config.api_headers(), json={"text": "Testing, one, two."}, timeout=60)
    on = requests.get(B + "/api/speaking", headers=config.api_headers()).json()["speaking"]
    requests.post(B + "/api/stop", headers=config.api_headers())
    off = not requests.get(B + "/api/speaking", headers=config.api_headers()).json()["speaking"]
    print("PASS" if on and off else "FAIL", "      speak → speaking → stop")
    passed += on and off
    print(f"\nserver: {passed}/{len(CASES) + 1} passed")


if __name__ == "__main__":
    main()

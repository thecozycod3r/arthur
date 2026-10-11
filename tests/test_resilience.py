"""Failure handling: broken sources, Claude down, concurrent rescans, timers across a server restart.
Backs up and restores the real briefing. Sends nothing. Run: .venv/bin/python tests/test_resilience.py"""
import os, shutil, subprocess, sys, time
from pathlib import Path
import requests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arthur import config, pipeline  # noqa: E402

B, H = "http://127.0.0.1:8730", config.api_headers()
res = []
def check(n, ok, note=""): res.append((n, bool(ok), note))

bk = config.DATA / "briefing.backup.json"
shutil.copy(pipeline.BRIEFING, bk)
try:
    # 1. one source crashes, one hangs -> briefing still produced, failures reported
    def boom(cfg, since): raise RuntimeError("simulated Gmail outage")
    def hang(cfg, since): time.sleep(30); return {"status": "ok", "detail": "", "items": [], "stats": {}}
    saved = dict(pipeline.SOURCES)
    pipeline.SOURCES.update(gmail=boom, slack=hang)
    pipeline.SCAN_TIMEOUT_S = 8
    t0 = time.time()
    out = pipeline.run()
    took = time.time() - t0
    s = out["sources"]
    check("crashed source reported as error, briefing still made", s["gmail"]["status"] == "error" and out["brief"].get("headline"))
    check("hung source times out instead of blocking", s["slack"]["status"] == "error" and "timed out" in s["slack"]["detail"], f"{took:.0f}s {s['slack']}")
    check("other sources unaffected", s["whatsapp"]["status"] == "ok" and s["calendar"]["status"] == "ok")
    pipeline.SOURCES.clear(); pipeline.SOURCES.update(saved); pipeline.SCAN_TIMEOUT_S = 90

    # 2. Claude unavailable -> rule-based fallback briefing
    cfgp = config.CONFIG_PATH; cfg_txt = cfgp.read_text()
    import json
    c = json.loads(cfg_txt); c["claude_bin"] = "/nonexistent/claude"; cfgp.write_text(json.dumps(c))
    try:
        out = pipeline.run()
        check("Claude down -> fallback briefing (quick mode)", out["engine"] == "fallback" and out["brief"]["spoken_briefing"])
    finally:
        cfgp.write_text(cfg_txt); os.chmod(cfgp, 0o600)

    # 3. two rescans at once -> second politely refused
    r1 = requests.post(B + "/api/refresh", headers=H).json(); r2 = requests.post(B + "/api/refresh", headers=H).json()
    check("concurrent rescan refused while one runs", r1.get("started") and not r2.get("started"), f"{r1} {r2}")
    for _ in range(90):
        if requests.get(B + "/api/status", headers=H).json().get("phase") in ("ready", "error"): break
        time.sleep(2)
finally:
    shutil.copy(bk, pipeline.BRIEFING); bk.unlink()

# 4. timer survives a server restart and still fires
requests.post(B + "/api/do/timer_set", json={"args": {"seconds": 12, "label": "restart-test"}}, headers=H)
subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.arthur.server"])
for _ in range(30):
    try: requests.get(B + "/api/status", headers=H, timeout=1); break
    except requests.RequestException: time.sleep(0.5)
pending = any(t["label"] == "restart-test" for t in requests.get(B + "/api/overview", headers=H).json()["timers"])
fired = False
t0 = time.time()
while time.time() - t0 < 20:
    if requests.get(B + "/api/speaking", headers=H).json()["speaking"]: fired = True; break
    time.sleep(0.3)
check("timer still pending after server restart", pending)
check("timer fires after the restart", fired)

for n, ok, note in res: print("PASS" if ok else "FAIL", n, f"[{note}]" if note and not ok else "")
print(f"\nresilience: {sum(r[1] for r in res)}/{len(res)} passed")

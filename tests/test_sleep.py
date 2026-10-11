"""Sleep mode: mic closes after the active window, opens on a wake request. Temporarily shortens the window."""
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arthur import config  # noqa: E402

uid = os.getuid()
def restart(): subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.arthur.ears"]); time.sleep(5)
def mode(): return config.read_json(config.DATA / "ears.json", {}).get("mode")
LOG = config.LOGS / "com.arthur.ears.err.log"
def mic_open():  # the listener logs every open/close; the last one wins (lsof can't tell: CoreAudio stays loaded)
    lines = [l for l in LOG.read_text().splitlines() if "listening for" in l or "sleeping: mic closed" in l]
    return bool(lines) and "listening for" in lines[-1]
cfg = json.loads(config.CONFIG_PATH.read_text()); orig = cfg.get("active_minutes", 10)
res = []
try:
    cfg["active_minutes"] = 0.25; config.CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False)); restart()
    res.append(("starts asleep, mic closed", mode() == "dormant" and not mic_open()))
    (config.DATA / "ears_wake").touch(); time.sleep(3)
    res.append(("'Listen' wakes him, mic open", mode() == "idle" and mic_open()))
    time.sleep(20)
    res.append(("falls asleep after the window, mic closed", mode() == "dormant" and not mic_open()))
finally:
    cfg["active_minutes"] = orig; config.CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False)); os.chmod(config.CONFIG_PATH, 0o600); restart()
for n, ok in res: print("PASS" if ok else "FAIL", n)
print(f"\nsleep: {sum(o for _, o in res)}/{len(res)} passed")

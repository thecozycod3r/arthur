"""JARVIS CLI.

  python -m arthur serve                 run the HUD server (launchd keeps this alive)
  python -m arthur run                   scan + analyze now, write briefing
  python -m arthur listen                "Hey Arthur" voice listener (launchd keeps this alive)
  python -m arthur train                 tune the "Hey Arthur" wake word to your voice (~3 min, in Terminal)
  python -m arthur morning               the 08:30 routine: open HUD, scan, speak
  python -m arthur doctor                show every channel's status
  python -m arthur auth google <label>   link a Google account (Gmail + Calendar), e.g. work / personal
  python -m arthur auth outlook          link Hotmail (needs outlook.client_id in config.json)
  python -m arthur install | uninstall   set up / remove the launchd agents
"""
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import config

config.LOGS.mkdir(exist_ok=True)
config.DATA.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(config.LOGS / "arthur.log"), logging.StreamHandler()],
)
log = logging.getLogger("arthur")

AGENTS = Path.home() / "Library/LaunchAgents"
LABEL_SERVER = "com.arthur.server"
LABEL_MORNING = "com.arthur.morning"
LABEL_EARS = "com.arthur.ears"
LABEL_MENU = "com.arthur.menubar"
LABEL_EVENING = "com.arthur.evening"


def url(cfg):
    return f"http://localhost:{cfg['port']}"


def server_up(cfg) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", cfg["port"])) == 0


def open_hud(cfg, boot=False):
    target = url(cfg) + ("/?boot=1" if boot else "/")
    chrome = "/Applications/Google Chrome.app"
    if Path(chrome).exists():
        subprocess.run(["open", "-na", chrome, "--args", f"--app={target}", "--window-size=1600,1000"])
    else:
        subprocess.run(["open", target])


def cmd_serve(cfg):
    import uvicorn
    uvicorn.run("arthur.server:app", host="127.0.0.1", port=cfg["port"], log_level="warning")


def cmd_morning(cfg):
    from . import pipeline
    from .server import say

    if not server_up(cfg):
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL_SERVER}"], check=False)
        for _ in range(20):
            if server_up(cfg):
                break
            time.sleep(0.5)
    pipeline.set_status("scanning", "Morning protocol initiated")
    open_hud(cfg, boot=True)
    out = pipeline.safe_run()
    time.sleep(2)  # let the HUD paint the fresh briefing before the voice starts
    text = out["brief"]["spoken_briefing"] if out else (
        f"Good morning, {cfg['user_name']}. I ran into trouble compiling today's briefing. Check the logs.")
    # speak through the server so the HUD's transcript and reactor follow the voice
    import requests
    try:
        requests.post(url(cfg) + "/api/speak", json={"text": text}, headers=config.api_headers(), timeout=5)
    except requests.RequestException:
        say(text).wait()
    (config.DATA / "ears_wake").touch()  # listen for 10 minutes so you can respond to the briefing
    os._exit(0)  # don't wait on a collector thread still parked on a privacy prompt


def cmd_doctor(cfg):
    from .collectors import google, slack, whatsapp

    since = config.since(cfg)
    checks = {"gmail": google.gmail, "calendar": google.calendar,
              "slack": slack.collect, "whatsapp": whatsapp.collect}
    for name, fn in checks.items():
        r = fn(cfg, since)
        mark = {"ok": "\033[32m●\033[0m", "offline": "\033[33m○\033[0m"}.get(r["status"], "\033[31m✕\033[0m")
        print(f"  {mark} {name:9} {r['status']:8} {len(r['items']):3} items  {r['detail']}")
    claude_ok = Path(cfg["claude_bin"]).exists()
    print(f"  {'●' if claude_ok else '✕'} {'claude':9} {cfg['claude_bin']}")
    print(f"  {'●' if server_up(cfg) else '○'} {'server':9} {url(cfg)}")


def cmd_auth(cfg, args):
    if not args:
        sys.exit("usage: auth google <label> | auth outlook")
    if args[0] == "google":
        from .collectors import google

        label = args[1] if len(args) > 1 else "personal"
        try:
            email = google.link_account(label)
        except RuntimeError as e:  # token is saved even if the API check fails (e.g. Gmail API not enabled yet)
            email = f"linked, but check failed: {str(e)[:160]}"
        if label not in cfg["google_accounts"]:
            cfg["google_accounts"].append(label)
            config.save(cfg)
        print(f"Linked Google '{label}' → {email}")
    elif args[0] == "outlook":
        from .collectors import outlook

        if not cfg["outlook"].get("client_id"):
            sys.exit("Set outlook.client_id in ~/arthur/config.json first (see SETUP.md).")
        who = outlook.link_account(cfg["outlook"]["client_id"])
        cfg["outlook"]["enabled"] = True
        config.save(cfg)
        print(f"Linked Hotmail → {who}")


def _plist(label, args, extra):
    py = str(config.ROOT / ".venv/bin/python")
    argv = "".join(f"<string>{a}</string>" for a in [py, "-m", "arthur", *args])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{argv}</array>
  <key>WorkingDirectory</key><string>{config.ROOT}</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>{Path.home()}</string>
  </dict>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>{config.LOGS}/{label}.out.log</string>
  <key>StandardErrorPath</key><string>{config.LOGS}/{label}.err.log</string>
  {extra}
</dict></plist>
"""


def cmd_install(cfg):
    AGENTS.mkdir(exist_ok=True)
    jobs = {
        LABEL_SERVER: _plist(LABEL_SERVER, ["serve"], "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"),
        LABEL_EARS: _plist(LABEL_EARS, ["listen"], "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
                           "<key>ProcessType</key><string>Interactive</string>"),
        LABEL_MENU: _plist(LABEL_MENU, ["menubar"], "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
                           "<key>ProcessType</key><string>Interactive</string>"),
        LABEL_EVENING: _plist(LABEL_EVENING, ["evening"],
                              "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>%d</integer>"
                              "<key>Minute</key><integer>%d</integer></dict>" % tuple(map(int, cfg.get("evening_time", "19:30").split(":")))),
        LABEL_MORNING: _plist(LABEL_MORNING, ["morning"],
                              "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>8</integer>"
                              "<key>Minute</key><integer>30</integer></dict>"),
    }
    uid = os.getuid()
    for label, xml in jobs.items():
        p = AGENTS / f"{label}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        p.write_text(xml)
        for attempt in range(10):  # bootout finishes asynchronously; retry until the label is free
            if subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise SystemExit(f"could not load {label}")
        print(f"installed {label}")
    print(f"HUD: {url(cfg)}  ·  morning briefing daily 08:30  ·  say \"Hey {cfg.get('assistant_name', 'Arthur')}\" any time")


def cmd_uninstall(cfg):
    for label in (LABEL_SERVER, LABEL_MORNING, LABEL_EARS, LABEL_MENU, LABEL_EVENING):
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        (AGENTS / f"{label}.plist").unlink(missing_ok=True)
        print(f"removed {label}")


def main():
    cfg = config.load()
    if not config.CONFIG_PATH.exists():
        config.save(cfg)
    cmd, *args = sys.argv[1:] or ["help"]
    if cmd == "serve":
        cmd_serve(cfg)
    elif cmd == "run":
        from . import pipeline

        out = pipeline.safe_run()
        if out:
            print(out["brief"]["headline"])
    elif cmd == "tts":  # render stdin to a wav, print its path (used so the voice model doesn't stay in RAM)
        from . import voice
        text = sys.stdin.read().strip()
        voice.prerender(text)
        print(voice.render(voice.sentences(text)[0]))
    elif cmd == "evening":
        from .evening import run as evening
        evening()
    elif cmd == "menubar":
        from .menubar import main as menubar
        menubar()
    elif cmd == "train":
        from .train import main as train
        train()
    elif cmd == "listen":
        from .ears import Ears

        Ears().run()
    elif cmd == "morning":
        cmd_morning(cfg)
    elif cmd == "doctor":
        cmd_doctor(cfg)
    elif cmd == "auth":
        cmd_auth(cfg, args)
    elif cmd == "open":
        open_hud(cfg)
    elif cmd == "install":
        cmd_install(cfg)
    elif cmd == "uninstall":
        cmd_uninstall(cfg)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

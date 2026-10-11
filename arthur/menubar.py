"""Menu bar icon showing whether Arthur is on, and what he's doing. Run by launchd: python -m arthur menubar

  A ◉   listening for "Hey Arthur"         A ●)) hearing you
  A …   thinking                           A ♪   speaking
  A ⏸   paused (you muted him)             A ✕   off — the listener or server isn't running
"""
import os
import subprocess
import time

import requests
import rumps

from . import config

BASE = f"http://127.0.0.1:{config.load()['port']}"
PAUSE = config.DATA / "ears_paused"
LOCKED = config.DATA / "screen_locked"
ICONS = {"dormant": "A ○", "idle": "A ◉", "listening": "A ●))", "confirm": "A ●))", "thinking": "A …", "speaking": "A ♪",
         "paused": "A ⏸", "off": "A ✕"}
TEXT = {"dormant": "Sleeping (mic off) — hold right ⌥ or click Listen", "idle": "Listening for “Hey Arthur”", "listening": "Hearing you…", "confirm": "Waiting for yes / no",
        "thinking": "Thinking…", "speaking": "Speaking", "paused": "Paused — not listening",
        "off": "Off — Arthur isn't running"}


def listener_running() -> bool:
    return subprocess.run(["pgrep", "-f", "arthur listen"], capture_output=True).returncode == 0


def current_state() -> str:
    if not listener_running():
        return "off"
    try:
        e = requests.get(f"{BASE}/api/ears", headers=config.api_headers(), timeout=1).json()
    except requests.RequestException:
        return "off"
    if PAUSE.exists() or LOCKED.exists():
        return "paused"
    mode = e.get("mode", "idle")
    if mode not in ("idle", "dormant") and time.time() - e.get("at", 0) > 60:  # stale state from an interrupted command
        mode = "idle"
    try:
        if requests.get(f"{BASE}/api/speaking", headers=config.api_headers(), timeout=1).json().get("speaking"):
            mode = "speaking"
    except requests.RequestException:
        pass
    return mode if mode in ICONS else "idle"


def usage() -> dict | None:
    return config.read_json(config.DATA / "usage.json")


def _when(ts) -> str:
    if not ts:
        return ""
    import datetime as dt
    try:
        t = dt.datetime.fromtimestamp(float(ts)) if not isinstance(ts, str) else dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ""
    t = t.astimezone() if t.tzinfo else t
    now = dt.datetime.now(t.tzinfo)
    fmt = "%-I:%M %p" if t.date() == now.date() else "%a %-I:%M %p"
    return " · resets " + t.strftime(fmt).lower()


class LockWatcher:
    """Tracks macOS screen lock/unlock as it happens (the static ioreg flag is unreliable)."""

    def __init__(self):
        from Foundation import NSDistributedNotificationCenter, NSObject
        import objc

        class _Obs(NSObject):
            @objc.typedSelector(b"v@:@")
            def locked_(self, _n):
                LOCKED.touch()

            @objc.typedSelector(b"v@:@")
            def unlocked_(self, _n):
                LOCKED.unlink(missing_ok=True)

        self.obs = _Obs.alloc().init()
        c = NSDistributedNotificationCenter.defaultCenter()
        c.addObserver_selector_name_object_(self.obs, "locked:", "com.apple.screenIsLocked", None)
        c.addObserver_selector_name_object_(self.obs, "unlocked:", "com.apple.screenIsUnlocked", None)
        LOCKED.unlink(missing_ok=True)  # the app starts at login, i.e. unlocked


class Menu(rumps.App):
    def __init__(self):
        super().__init__("Arthur", title=ICONS["idle"], quit_button=None)
        self.lock_watcher = LockWatcher()
        self.status = rumps.MenuItem(TEXT["idle"])
        self.pause_item = rumps.MenuItem("Pause listening", callback=self.toggle_pause)
        self.u5 = rumps.MenuItem("Claude session: waiting for first reading")
        self.menu = [
            self.status, self.u5, None,
            rumps.MenuItem("Listen for 10 minutes", callback=lambda _: (config.DATA / "ears_wake").touch()),
            rumps.MenuItem("Open dashboard", callback=self.open_hud),
            rumps.MenuItem("Brief me", callback=lambda _: self.post("/api/speak")),
            rumps.MenuItem("Stop talking", callback=lambda _: self.post("/api/stop")),
            rumps.MenuItem("Rescan everything", callback=lambda _: self.post("/api/refresh")),
            None,
            self.pause_item,
            rumps.MenuItem("Restart Arthur", callback=self.restart),
            rumps.MenuItem("Hold right ⌥ to talk", callback=None),
        ]

    @rumps.timer(2)  # every 2 s is plenty for a status icon, and halves its CPU
    def tick(self, _):
        s = current_state()
        u = usage()
        pct = ""
        if u:
            p5 = u["five_hour"]["pct"]
            pct = f" {p5}%"
            self.u5.title = f"Claude session: {p5}% used{_when(u['five_hour'].get('resets'))}"
        self.title = ICONS[s] + pct
        self.status.title = TEXT[s]
        self.pause_item.title = "Resume listening" if s == "paused" else "Pause listening"

    def post(self, path):
        try:
            requests.post(BASE + path, json={}, headers=config.api_headers(), timeout=5)
        except requests.RequestException:
            rumps.notification("Arthur", "", "The server isn't responding — try Restart Arthur.")

    def open_hud(self, _):
        from .__main__ import open_hud
        open_hud(config.load())

    def toggle_pause(self, _):
        if PAUSE.exists():
            PAUSE.unlink()
        else:
            PAUSE.touch()

    def restart(self, _):
        uid = os.getuid()
        for label in ("com.arthur.server", "com.arthur.ears"):
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])
        rumps.notification("Arthur", "", "Restarting — back in a few seconds.")


def main():
    Menu().run()

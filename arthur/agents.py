"""Claude Code by voice.

- open_session(): opens a Terminal window running an interactive `claude` session with the prompt typed in.
  Your normal permission prompts apply — you watch and approve.
- start_task(): runs `claude -p` in the background (edits allowed, anything else follows your settings'
  allow rules), then Arthur speaks a one-line result when it finishes.
"""
import json
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import config

TASKS = config.DATA / "tasks"
NO_DELETE = ("This task was started by voice. Do not delete anything (files, folders, git history, data, messages, "
             "cloud resources). If the task needs a deletion, stop short of it and list exactly what would need "
             "deleting so the user can approve it at the laptop. ")
SPOKEN_HINT = NO_DELETE + ("When you finish, end your final message with one line starting 'SPOKEN:' that sums up the outcome "
               "in one plain sentence for a voice assistant to read aloud.")


def open_session(project: str, prompt: str) -> None:
    cfg = config.load()
    prompt = prompt + "\n\n(Started by voice: don't delete anything without asking me first in this session.)"
    cmd = f"cd {shlex.quote(project)} && {shlex.quote(cfg['claude_bin'])} {shlex.quote(prompt)}"
    script = 'on run argv\n tell application "Terminal"\n  activate\n  do script (item 1 of argv)\n end tell\nend run'
    subprocess.run(["osascript", "-e", script, cmd], check=True)  # argv: prompts with ’ or — broke json-quoted AppleScript


def start_task(project: str, prompt: str, on_done=None) -> str:
    cfg = config.load()
    TASKS.mkdir(parents=True, exist_ok=True)
    tid = datetime.now().strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    rec = {"id": tid, "project": project, "name": Path(project).name, "prompt": prompt,
           "status": "running", "started": datetime.now().isoformat()}
    _save(rec)

    def run():
        t0 = time.time()
        try:
            p = subprocess.run(
                [cfg["claude_bin"], "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
                 "--append-system-prompt", SPOKEN_HINT],
                cwd=project, capture_output=True, text=True, timeout=60 * 45,
            )
            res = json.loads(p.stdout or "{}")
            text = res.get("result") or p.stderr[-500:]
            spoken = next((ln.split(":", 1)[1].strip() for ln in reversed(text.splitlines())
                           if ln.strip().upper().startswith("SPOKEN:")), text.strip().splitlines()[-1][:200] if text.strip() else "")
            rec.update(status="error" if res.get("is_error") or p.returncode else "done", result=text[-4000:], spoken=spoken)
        except subprocess.TimeoutExpired:
            rec.update(status="error", spoken="It ran out of time after forty five minutes.")
        except Exception as e:  # noqa: BLE001
            rec.update(status="error", spoken=f"It failed to start: {str(e)[:120]}")
        rec.update(finished=datetime.now().isoformat(), took_min=round((time.time() - t0) / 60, 1))
        _save(rec)
        if on_done:
            on_done(rec)

    threading.Thread(target=run, daemon=True).start()
    return tid


def _save(rec):
    config.write_json(TASKS / f"{rec['id']}.json", rec)


def summary(limit: int = 8) -> list[dict]:
    if not TASKS.exists():
        return []
    recs = [config.read_json(p, {}) for p in sorted(TASKS.glob("*.json"), reverse=True)[:limit]]
    return [{k: r.get(k) for k in ("id", "name", "prompt", "status", "started", "took_min", "spoken")} for r in recs]

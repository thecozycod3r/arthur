"""Paths + config loading. All user-editable settings live in ~/arthur/config.json."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SECRETS = ROOT / "secrets"
LOGS = ROOT / "logs"
UI = ROOT / "ui"
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    "user_name": "Tony",
    "assistant_name": "Arthur",
    "people": {},          # name -> note, e.g. {"Pepper": "woman (she/her)"}
    "mute": [],            # case-insensitive words; matching senders/subjects never reach the briefing
    "port": 8730,
    "voice": "Daniel",
    "model": "sonnet",
    "claude_bin": str(Path.home() / ".local/bin/claude"),
    "lookback_hours": 24,
    "monday_lookback_hours": 72,
    "about_me": "",
    "vips": [],
    "google_accounts": [],
    "outlook": {"client_id": "", "enabled": False},
    "slack": {"workspaces": []},
    "whatsapp": {"enabled": True, "max_chats": 40, "msgs_per_chat": 8, "skip_muted_groups": True},
}


TOKEN_FILE = SECRETS / "api_token"


def api_token() -> str:
    """Shared secret for Arthur's local API (created once, readable only by you)."""
    import os
    import secrets
    if not TOKEN_FILE.exists():
        SECRETS.mkdir(mode=0o700, exist_ok=True)
        TOKEN_FILE.write_text(secrets.token_hex(32))
        os.chmod(TOKEN_FILE, 0o600)
    return TOKEN_FILE.read_text().strip()


def api_headers() -> dict:
    return {"X-Arthur-Token": api_token()}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def since(cfg: dict) -> datetime:
    now = datetime.now(timezone.utc)
    hours = cfg["monday_lookback_hours"] if datetime.now().weekday() == 0 else cfg["lookback_hours"]
    return now - timedelta(hours=hours)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(path)

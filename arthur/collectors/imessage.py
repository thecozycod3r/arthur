"""iMessage / SMS, read-only from the Messages app database (~/Library/Messages/chat.db).

Needs Full Disk Access for Arthur's python (already granted for WhatsApp). Replies go out through the
Messages app via AppleScript, and only after a spoken/clicked yes.
"""
import glob
import re
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import error, offline, ok

DB = Path.home() / "Library/Messages/chat.db"
APPLE_EPOCH = 978307200
SENDER_ID = re.compile(r"^[A-Z]{2}-[A-Z0-9]+$|^[A-Za-z]{4,}$|^\d{3,6}$")  # bank/OTP sender IDs and short codes


def _text(text, body) -> str:
    """Newer macOS keeps the text only inside attributedBody (an NSAttributedString typedstream)."""
    if text:
        return text
    if not body:
        return ""
    i = body.find(b"NSString")
    if i < 0:
        return ""
    i = body.find(b"\x2b", i) + 1
    n = body[i]
    i += 1
    if n == 0x81:
        n, i = int.from_bytes(body[i:i + 2], "little"), i + 2
    elif n == 0x82:
        n, i = int.from_bytes(body[i:i + 3], "little"), i + 3
    return body[i:i + n].decode("utf-8", "ignore")


def _contacts() -> dict:
    """last-10-digits / email -> name, from the macOS Contacts database."""
    names = {}
    for db in glob.glob(str(Path.home() / "Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb")):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            for num, first, last, org in c.execute(
                "SELECT p.ZFULLNUMBER, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION FROM ZABCDPHONENUMBER p "
                "JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK"):
                name = " ".join(x for x in (first, last) if x) or org
                if num and name:
                    names[re.sub(r"\D", "", num)[-10:]] = name
            for em, first, last, org in c.execute(
                "SELECT e.ZADDRESS, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION FROM ZABCDEMAILADDRESS e "
                "JOIN ZABCDRECORD r ON e.ZOWNER = r.Z_PK"):
                name = " ".join(x for x in (first, last) if x) or org
                if em and name:
                    names[em.lower()] = name
            c.close()
        except sqlite3.Error:
            continue
    return names


def _name(handle: str, names: dict) -> str:
    if not handle:
        return "Unknown"
    if "@" in handle:
        return names.get(handle.lower(), handle)
    return names.get(re.sub(r"\D", "", handle)[-10:], handle)


def collect(cfg, since: datetime) -> dict:
    if not cfg.get("imessage", {}).get("enabled", True):
        return offline("iMessage disabled in config")
    if not DB.exists():
        return offline("Messages app not set up")
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        return error(f"cannot open Messages DB ({e}) — Full Disk Access needed")
    try:
        cutoff = int((since.astimezone(timezone.utc).timestamp() - APPLE_EPOCH) * 1e9)
        rows = con.execute(
            """
            SELECT m.date, m.is_from_me, m.text, m.attributedBody, h.id, c.ROWID, c.display_name,
                   c.chat_identifier, c.style, m.is_read
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.date > ? AND m.associated_message_type = 0
            ORDER BY m.date
            """, (cutoff,)).fetchall()
    except sqlite3.Error as e:
        return error(f"query failed: {e}")
    finally:
        con.close()

    names = _contacts()
    chats = defaultdict(lambda: {"msgs": [], "incoming": 0, "unread": 0})
    skipped = 0
    for date, from_me, text, body, handle, cid, display, ident, style, is_read in rows:
        if SENDER_ID.match(ident or ""):
            skipped += 1  # OTPs, bank alerts, delivery updates
            continue
        c = chats[cid]
        group = style == 43
        c.update(ident=ident, group=group, last=date, last_from_me=bool(from_me),
                 name=display or _name(ident, names))
        msg = _text(text, body).strip() or "[attachment]"
        if from_me:
            c["msgs"].append(("You", msg))
        else:
            c["incoming"] += 1
            c["unread"] += not is_read
            c["msgs"].append((_name(handle, names) if group else "", msg))

    items = []
    for cid, c in sorted(chats.items(), key=lambda kv: kv[1]["last"], reverse=True)[:25]:
        if not c["incoming"]:
            continue
        flags = (["unread"] if c["unread"] else []) + (["awaiting-reply"] if not c["last_from_me"] and not c["group"] else [])
        if c["group"]:
            flags.append("group")
        items.append({
            "id": f"im:{cid}",
            "source": "imessage",
            "account": "Messages",
            "kind": "group" if c["group"] else "chat",
            "from": c["name"],
            "title": c["name"],
            "text": "\n".join(f"{w}: {t}"[:300] if w else t[:300] for w, t in c["msgs"][-8:]),
            "time": datetime.fromtimestamp(c["last"] / 1e9 + APPLE_EPOCH).astimezone().isoformat(),
            "link": "messages://",
            "flags": flags,
            "meta": {"handle": c["ident"], "group": c["group"]},
        })
    return ok(items, {"scanned": sum(c["incoming"] for c in chats.values()), "skipped_alerts": skipped})


def send(handle: str, text: str) -> None:
    """Send through the Messages app. Only called after an explicit yes."""
    script = (
        'on run argv\n'
        ' tell application "Messages"\n'
        '  set svc to 1st account whose service type = iMessage\n'
        '  send (item 2 of argv) to participant (item 1 of argv) of svc\n'
        ' end tell\n'
        'end run')
    p = subprocess.run(["osascript", "-e", script, handle, text], capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[:200] or "Messages refused to send")

"""WhatsApp, read-only, straight from the WhatsApp Desktop (Mac) local database.

No unofficial API, no automation of the WhatsApp account — just reads what the
desktop app has already synced. Needs the Mac WhatsApp app installed and signed in.
"""
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import error, offline, ok

WA_DIR = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
DB = WA_DIR / "ChatStorage.sqlite"
CONTACTS = WA_DIR / "ContactsV2.sqlite"
APPLE_EPOCH = 978307200  # Core Data timestamps count from 2001-01-01
MEDIA = {1: "[photo]", 2: "[video]", 3: "[voice note]", 4: "[contact]", 5: "[location]",
         8: "[document]", 15: "[sticker]", 11: "[gif]"}
SKIP_TYPES = {6, 10, 14, 59, 66}  # system/group events, reactions-ish noise


def _names(con) -> dict:
    """jid/lid -> best human name: address book first, then WhatsApp push names."""
    names = {}
    try:
        for jid, push in con.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME"):
            if jid and push:
                names[jid] = f"~{push}"
    except sqlite3.Error:
        pass
    try:
        c = sqlite3.connect(f"file:{CONTACTS}?mode=ro", uri=True, timeout=5)
        for lid, wid, full in c.execute("SELECT ZLID, ZWHATSAPPID, ZFULLNAME FROM ZWAADDRESSBOOKCONTACT"):
            for k in (lid, wid):
                if k and full:
                    names[k] = full
        c.close()
    except sqlite3.Error:
        pass
    return names


def collect(cfg, since: datetime) -> dict:
    w = cfg["whatsapp"]
    if not w.get("enabled"):
        return offline("WhatsApp disabled in config")
    if not DB.exists():
        return offline("WhatsApp Desktop not installed")
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        return error(f"cannot open DB ({e}) — grant Full Disk Access to Jarvis's python")
    try:
        cutoff = since.astimezone(timezone.utc).timestamp() - APPLE_EPOCH
        now_apple = datetime.now(timezone.utc).timestamp() - APPLE_EPOCH
        names = _names(con)
        muted = {jid for (jid,) in con.execute(
            "SELECT ZJID FROM ZWACHATPUSHCONFIG WHERE ZMUTEDUNTIL > ?", (now_apple,))}
        rows = con.execute(
            """
            SELECT m.ZISFROMME, m.ZMESSAGEDATE, m.ZTEXT, m.ZMESSAGETYPE, m.ZPUSHNAME,
                   s.Z_PK, s.ZPARTNERNAME, s.ZCONTACTJID, s.ZSESSIONTYPE, s.ZUNREADCOUNT, s.ZARCHIVED,
                   gm.ZMEMBERJID
            FROM ZWAMESSAGE m
            JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
            LEFT JOIN ZWAGROUPMEMBER gm ON m.ZGROUPMEMBER = gm.Z_PK
            WHERE m.ZMESSAGEDATE > ? AND s.ZSESSIONTYPE IN (0, 1)
            ORDER BY m.ZMESSAGEDATE
            """,
            (cutoff,),
        ).fetchall()
    except sqlite3.Error as e:
        return error(f"query failed: {e}")
    finally:
        con.close()

    chats = defaultdict(lambda: {"msgs": [], "last_from_me": False, "incoming": 0})
    for (from_me, mdate, text, mtype, push, sid, partner, jid, stype, unread, archived, member_jid) in rows:
        if mtype in SKIP_TYPES:
            continue
        c = chats[sid]
        c.update(name=partner or names.get(jid, jid), jid=jid, group=stype == 1, unread=unread or 0,
                 archived=bool(archived), muted=jid in muted)
        c["last_from_me"] = bool(from_me)
        c["last"] = mdate
        if from_me:
            c["msgs"].append(("You", text or MEDIA.get(mtype, "[media]")))
            continue
        c["incoming"] += 1
        who = names.get(member_jid, "Someone") if stype == 1 else (partner or "")
        c["msgs"].append((who, text or MEDIA.get(mtype, "[media]")))

    items, skipped_muted = [], 0
    ranked = sorted(chats.items(), key=lambda kv: kv[1].get("last", 0), reverse=True)
    for sid, c in ranked:
        if c["incoming"] == 0:
            continue
        if c["group"] and c["muted"] and w.get("skip_muted_groups", True) and c["unread"] == 0:
            skipped_muted += 1
            continue
        if len(items) >= w.get("max_chats", 40):
            break
        tail = c["msgs"][-w.get("msgs_per_chat", 8):]
        flags = []
        if c["unread"]:
            flags.append("unread")
        if not c["last_from_me"] and not c["group"]:
            flags.append("awaiting-reply")
        if c["group"]:
            flags.append("group")
        if c["muted"]:
            flags.append("muted")
        items.append({
            "id": f"wa:{sid}",
            "source": "whatsapp",
            "account": "WhatsApp",
            "kind": "group" if c["group"] else "chat",
            "from": c["name"],
            "title": c["name"],
            "text": "\n".join(f"{w_}: {t}"[:300] if w_ else t[:300] for w_, t in tail),
            "time": datetime.fromtimestamp(c["last"] + APPLE_EPOCH).astimezone().isoformat(),
            "link": f"whatsapp://send?phone={c['jid'].split('@')[0]}" if not c["group"] else "whatsapp://",
            "flags": flags,
            "unread": c["unread"],
            "meta": {"jid": c["jid"], "group": c["group"]},
            "incoming": c["incoming"],
        })
    return ok(items, {
        "scanned": sum(c["incoming"] for c in chats.values()),
        "chats": len(chats),
        "unread": sum(c.get("unread", 0) for c in chats.values()),
        "skipped_muted": skipped_muted,
    })


def compose_link(jid: str, text: str) -> str | None:
    """Deep link that opens WhatsApp Desktop on this chat with the message typed in (you press Enter)."""
    from urllib.parse import quote

    if not jid or not jid.endswith("@s.whatsapp.net"):
        return None  # groups and privacy-ID contacts can't be deep-linked
    return f"whatsapp://send?phone={jid.split('@')[0]}&text={quote(text)}"


# ---------- texting anyone (contacts + send) ----------
def directory() -> list[dict]:
    """Everyone you can message: name, phone JID (deep-linkable) and privacy-ID (LID), from WhatsApp's own contact DB."""
    people, seen = [], set()
    try:
        c = sqlite3.connect(f"file:{CONTACTS}?mode=ro", uri=True, timeout=5)
        for name, wid, lid in c.execute("SELECT ZFULLNAME, ZWHATSAPPID, ZLID FROM ZWAADDRESSBOOKCONTACT "
                                        "WHERE ZFULLNAME IS NOT NULL AND ZWHATSAPPID LIKE '%@s.whatsapp.net'"):
            if wid not in seen:
                seen.add(wid)
                people.append({"name": name.strip(), "jid": wid, "lid": lid})
        c.close()
    except sqlite3.Error:
        pass
    return people


def sounds(x: str) -> str:
    """Rough phonetic key so spoken spellings match saved ones: Steven ~ Stephen, Jon ~ John."""
    import re as _re
    x = x.lower()
    for a_, b_ in (("th", "t"), ("dh", "d"), ("bh", "b"), ("kh", "k"), ("ph", "f"), ("ck", "k"), ("q", "k"),
                   ("c", "k"), ("w", "v"), ("z", "s"), ("ee", "i"), ("ea", "i"), ("oo", "u"), ("aa", "a"), ("y", "i")):
        x = x.replace(a_, b_)
    return _re.sub(r"(.)\1+", r"\1", _re.sub(r"[^a-z ]", "", x))


def find(name: str, limit: int = 4) -> list[dict]:
    """Fuzzy name lookup on spelling and sound. Exact/first-name matches first, then similarity."""
    from difflib import SequenceMatcher
    from .. import config as _cfg
    # nicknames: "Natasha" -> the contact saved as "Nat 🕷 (work)". Matched by sound, so
    # however whisper spells the nickname (Natascha, Natasha) it still resolves.
    qs = sounds(name.strip())
    direct = max((max(SequenceMatcher(a=qs, b=sounds(p["name"])).ratio(),
                      SequenceMatcher(a=qs, b=(sounds(p["name"]).split() or [""])[0]).ratio()) for p in directory()), default=0)
    for nick, saved in (_cfg.load().get("aliases") or {}).items():
        a_sim = SequenceMatcher(a=qs, b=sounds(nick)).ratio()
        if a_sim >= 0.85 and a_sim >= direct:  # a real contact that matches better wins (Nat vs Natasha)
            target = sounds(saved)
            hit = max(directory(), key=lambda p: SequenceMatcher(a=target, b=sounds(p["name"])).ratio(), default=None)
            if hit and SequenceMatcher(a=target, b=sounds(hit["name"])).ratio() >= 0.9:
                return [hit | {"score": 1.0, "name": f"{nick} ({hit['name']})"}]
    q = sounds(name.strip())
    scored = []
    for p in directory():
        n = sounds(p["name"])
        first = n.split()[0] if n.split() else n
        if q == n or q == first:
            s = 1.0
        elif n.startswith(q) or q in n.split():
            s = 0.9
        else:
            s = max(SequenceMatcher(a=q, b=n).ratio(), SequenceMatcher(a=q, b=first).ratio())
        if s >= 0.72:
            scored.append((s, p))
    scored.sort(key=lambda x: (-x[0], len(x[1]["name"])))
    return [p | {"score": round(s, 2)} for s, p in scored[:limit]]


def phone_jid(jid: str) -> str | None:
    """Chat JID -> phone JID. Chats keyed by privacy-ID (@lid) are mapped through the contacts DB."""
    if jid.endswith("@s.whatsapp.net"):
        return jid
    if jid.endswith("@lid"):
        return next((p["jid"] for p in directory() if p.get("lid") == jid), None)
    return None


def _last_sent(phone: str, lid: str | None) -> tuple[float, str] | None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(
            "SELECT m.ZMESSAGEDATE, m.ZTEXT FROM ZWAMESSAGE m JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK "
            "WHERE m.ZISFROMME = 1 AND s.ZCONTACTJID IN (?, ?) ORDER BY m.ZMESSAGEDATE DESC LIMIT 1",
            (phone, lid or "")).fetchone()
    finally:
        con.close()


def send(phone: str, text: str, lid: str | None = None) -> dict:
    """Open the chat with the text typed, check the box holds exactly that text, press WhatsApp's Send button
    via Accessibility (no keystrokes), then confirm in WhatsApp's own DB that it went out.
    Only ever called after an explicit yes."""
    import time
    from urllib.parse import quote
    from .. import axwa
    before = _last_sent(phone, lid)
    subprocess.run(["open", f"whatsapp://send?phone={phone.split('@')[0]}&text={quote(text)}"], check=True)
    for _ in range(25):  # wait for the chat to open with our text in the box
        time.sleep(0.2)
        if axwa.compose_text() == text.strip():
            break
    r = axwa.press_send(text)
    if not r.get("ok"):
        return {"ok": False, "typed": True, "error": r.get("error")}
    for _ in range(16):  # confirm from WhatsApp's own records
        time.sleep(0.5)
        last = _last_sent(phone, lid)
        if last and last != before and (last[1] or "").strip() == text.strip():
            return {"ok": True, "verified": True}
    return {"ok": True, "verified": False}

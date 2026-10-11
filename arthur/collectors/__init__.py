"""Each collector returns {"status": ok|offline|error, "detail": str, "items": [...], "stats": {...}}.

Item shape (shared across sources so the triage prompt and UI treat them alike):
  id, source, account, kind, from, title, text, time (ISO), link, flags (list[str])
"""


def offline(detail: str) -> dict:
    return {"status": "offline", "detail": detail, "items": [], "stats": {}}


def error(detail: str) -> dict:
    return {"status": "error", "detail": detail, "items": [], "stats": {}}


def ok(items: list, stats: dict | None = None, detail: str = "") -> dict:
    return {"status": "ok", "detail": detail, "items": items, "stats": stats or {}}

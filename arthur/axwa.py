"""Drive WhatsApp Desktop through macOS Accessibility: read the compose box, press the Send button.
No keystrokes are ever sent, so nothing can land in another app. Needs Accessibility for Arthur's python."""
import time

from AppKit import NSRunningApplication, NSWorkspace
from ApplicationServices import (AXIsProcessTrusted, AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
                                 AXUIElementPerformAction, AXUIElementSetAttributeValue)

BUNDLE = "net.whatsapp.WhatsApp"


def trusted() -> bool:
    return bool(AXIsProcessTrusted())


def _app():
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE)
    return AXUIElementCreateApplication(apps[0].processIdentifier()) if apps else None


def _attr(el, name):
    err, v = AXUIElementCopyAttributeValue(el, name, None)
    return v if err == 0 else None


def _clean(s) -> str:
    return (s or "").replace("‎", "").replace("‏", "").strip()


def find(role: str, desc: str, timeout: float = 4.0):
    """Breadth-first search of WhatsApp's front window for an element (e.g. AXButton 'Send')."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        app = _app()
        win = _attr(app, "AXFocusedWindow") or (_attr(app, "AXWindows") or [None])[0] if app else None
        queue, seen = [win] if win else [], 0
        while queue and seen < 6000:
            el = queue.pop(0)
            seen += 1
            if _attr(el, "AXRole") == role and _clean(_attr(el, "AXDescription")) == desc:
                return el
            queue.extend(_attr(el, "AXChildren") or [])
        time.sleep(0.25)
    return None


def compose_text() -> str | None:
    box = find("AXTextArea", "Compose message", 1.0)
    return _clean(_attr(box, "AXValue")) if box else None


def clear_compose() -> bool:
    box = find("AXTextArea", "Compose message", 2.0)
    return bool(box) and AXUIElementSetAttributeValue(box, "AXValue", "") == 0


def press_send(expected_text: str) -> dict:
    """Press Send only if the compose box holds exactly the approved text."""
    if not trusted():
        return {"ok": False, "error": "Accessibility permission missing for Arthur's python"}
    box = find("AXTextArea", "Compose message")
    if not box:
        return {"ok": False, "error": "couldn't find WhatsApp's message box"}
    typed = _clean(_attr(box, "AXValue"))
    if typed != _clean(expected_text):
        return {"ok": False, "error": f"message box holds something else ({typed[:40]!r}) — not sending"}
    btn = find("AXButton", "Send")
    if not btn:
        return {"ok": False, "error": "couldn't find WhatsApp's Send button"}
    return {"ok": AXUIElementPerformAction(btn, "AXPress") == 0}

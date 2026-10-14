"""Always-on "Hey Arthur" listener. Fully offline: Vosk spots the wake phrase, faster-whisper transcribes the
command, commands.decide() picks the action. Slack replies are read back and only sent after a spoken "yes".

Run by launchd (com.arthur.ears) via:  python -m arthur listen
"""
import collections
import json
import logging
import queue
import re
import subprocess
import threading
import time

import numpy as np
import requests
import sounddevice as sd

from . import commands, config, hearing, voiceprint

log = logging.getLogger("arthur.ears")
RATE = 16000
BLOCK = 2000  # 125 ms
STATE = config.DATA / "ears.json"
VOSK_MODEL = config.ROOT / "models/vosk-model-small-en-us-0.15"
WHISPER_DIR = config.ROOT / "models/whisper"
CHIME = "/System/Library/Sounds/Tink.aiff"
CUE = "/System/Library/Sounds/Pop.aiff"  # soft: "I'm still listening"
YES = re.compile(r"\b(yes|yeah|yep|send|send it|go ahead|do it|confirm|sure|please)\b", re.I)


class Streamer:
    """Speaks an answer sentence by sentence while Claude is still writing it."""

    def __init__(self, ears):
        self.ears, self.sent, self.started = ears, 0, False

    def feed(self, action, speech):
        if action != "answer" or not speech:
            return
        ends = [m.end() for m in re.finditer(r"[.!?](?=\s)", speech)]
        cut = ends[-1] if ends else 0
        new = speech[self.sent:cut].strip()
        if not new or (not self.started and len(new) < 12):
            return
        self.ears.post("/api/speak/append" if self.started else "/api/speak",
                       {"text": new} if self.started else {"text": new, "stream": True})
        self.started, self.sent = True, cut

    def close(self, d):
        if self.started:
            rest = (d.get("speech") or "")[self.sent:].strip() if d.get("action") == "answer" else ""
            self.ears.post("/api/speak/append", {"text": rest, "final": True})


ENDERS = re.compile(r"^\W*(that'?s (all|it)|thanks?( you)?|no,? thanks?|never ?mind|good ?bye|bye|nothing|i'?m good)\W*$", re.I)

NO = re.compile(r"\b(no|nope|don'?t|do not|cancel|stop|wait|hold on|never ?mind|not yet)\b", re.I)


def classify_answer(text: str) -> str:
    """Spoken reply to "Shall I send it?" -> 'yes' | 'draft' | 'no'. Anything unclear is a no."""
    t = (text or "").lower()
    if "draft" in t and not re.search(r"\b(don'?t|do not|no)\s+(draft|save)", t):
        return "draft"  # "draft it, don't send it" means draft, not no
    if NO.search(t):
        return "no"
    return "yes" if YES.search(t) else "no"


PAUSE = config.DATA / "ears_paused"
WAKE = config.DATA / "ears_wake"  # touch to wake the listener for 10 minutes
ASK = config.DATA / "ask.json"            # a typed command from the dashboard
CONFIRM = config.DATA / "confirm.json"    # a typed command waiting for an on-screen yes / draft / no
ANSWER = config.DATA / "confirm_answer.json"
LOCKED = config.DATA / "screen_locked"  # maintained by the menu bar app from macOS lock/unlock events


def candidate_re():
    """Vosk phrases that wake him. Default: exactly "hey arthur" — on the owner's recorded voice this caught 15/19 with
    0 false wakes across 65 look-alikes, while adding a whisper double-check dropped it to 3/19 (whisper hears his
    "Arthur" as "otto"). With wake_verify on, sound-alikes become candidates and whisper decides (old behaviour)."""
    cfg = config.load()
    # "author": live in the owner's room Vosk heard his "Hey Arthur" as "hey author" 3/3 times (11 Oct)
    words = ["arthur", "author", *cfg.get("wake_vosk_extra", [])]
    if cfg.get("wake_verify", False):
        words += ["other", "archer"]
    return re.compile(r"\bhey (" + "|".join(map(re.escape, words)) + r")\b")


def verify_re():
    """Whisper spellings accepted as the name (plus any learned from your voice)."""
    extra = "".join("|\\b" + re.escape(w) + "\\b" for w in config.load().get("wake_whisper_extra", []))
    return re.compile(r"\barth[ue]r\b|\bartur\b|\bhey\W{0,3}author\b" + extra)


CANDIDATE = candidate_re()
VERIFY = verify_re()


def correct(text: str) -> str:
    """Apply learned fixes for words whisper mishears in your voice (from `arthur train`)."""
    fixes = config.load().get("corrections") or {}
    if not fixes:
        return text
    return re.sub(r"[A-Za-z']+", lambda m: fixes.get(m.group(0).lower(), m.group(0)), text)


def vocabulary() -> str:
    """Names whisper should expect: config vocabulary + people/clients in today's briefing."""
    cfg = config.load()
    words = list(cfg.get("vocabulary") or [])
    b = config.read_json(config.DATA / "briefing.json", {}) or {}
    for v in (b.get("index") or {}).values():
        name = re.sub(r"<.*?>|\(.*?\)|[\u202a-\u202e]", "", v.get("from") or "").strip(' "')
        if name and not re.search(r"\d{4}|@|noreply", name, re.I):
            words.append(name[:30])
    seen, out = set(), []
    for w in ["Arthur", "Slack", "WhatsApp", "Gmail", "Claude", *words]:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return ", ".join(out)[:600] + "."


AWAKE = {"until": 0.0}  # shared with the dashboard: when the mic closes again
CONVO = config.DATA / "conversation.jsonl"


def state(mode: str, heard: str = "", reply: str = ""):
    config.write_json(STATE, {"mode": mode, "heard": heard, "reply": reply, "at": time.time(), "awake_until": AWAKE["until"]})


def log_turn(you: str, arthur: str, action: str):
    """Conversation history for the dashboard (last 200 turns kept)."""
    import json as _j
    line = _j.dumps({"at": time.time(), "you": you, "arthur": arthur, "action": action})
    old = CONVO.read_text().splitlines()[-199:] if CONVO.exists() else []
    CONVO.write_text("\n".join(old + [line]) + "\n")


class Ears:
    def __init__(self):
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        cfg = config.load()
        self.base = f"http://127.0.0.1:{cfg['port']}"
        wake = cfg.get("assistant_name", "Arthur").lower()
        self.phrase = f"hey {wake}"
        self.vosk = Model(str(VOSK_MODEL))
        # decoys give sound-alikes somewhere else to land, so they don't get forced into the wake phrase
        decoys = ["hey", "hey there", "author", "archer", "other", "are you", "art", "harder", "hey author",
                  "hey archer", "hey other", "okay", "hi", "father", "mother", "after", "order"]
        self.grammar = json.dumps([self.phrase, *decoys, "[unk]"])
        self.ring = collections.deque(maxlen=int(2.8 * RATE / BLOCK))  # last ~2.8 s, for the whisper double-check
        self.Rec = KaldiRecognizer
        self.rec = KaldiRecognizer(self.vosk, RATE, self.grammar)
        self._whisper, self._whisper_used = None, 0.0  # loaded on first use, dropped after 10 idle minutes
        self.q: queue.Queue = queue.Queue()
        self.noise = 300.0
        self.ptt_held, self.ptt_pressed = False, False
        self._start_ptt(cfg.get("ptt_key", "alt_r"))

    # ---------- push-to-talk ----------
    def _start_ptt(self, key_name: str):
        """Hold the key (default: right Option) to talk — no wake word. Needs Input Monitoring permission for python."""
        if not key_name:
            return
        try:
            from pynput import keyboard
        except ImportError:
            return
        target = getattr(keyboard.Key, key_name, None)

        def down(k):
            if k == target and not self.ptt_held:
                self.ptt_held = self.ptt_pressed = True
                log.info("talk key down")

        def up(k):
            if k == target:
                self.ptt_held = False

        keyboard.Listener(on_press=down, on_release=up, daemon=True).start()
        log.info("push-to-talk: hold %s", key_name)

    def record_held(self, max_s=30.0) -> np.ndarray:
        """Record while the push-to-talk key is held (plus a short tail)."""
        chunks, t0 = [], time.time()
        while (self.ptt_held or not chunks) and time.time() - t0 < max_s:
            try:
                chunks.append(np.frombuffer(self.q.get(timeout=1), dtype=np.int16))
            except queue.Empty:
                break
        for _ in range(2):  # 250 ms tail so the last word isn't clipped
            try:
                chunks.append(np.frombuffer(self.q.get(timeout=0.5), dtype=np.int16))
            except queue.Empty:
                break
        return np.concatenate(chunks) if chunks else np.array([], dtype=np.int16)

    # ---------- audio ----------
    def _cb(self, data, frames, t, status):
        self.q.put(bytes(data))

    def _drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    def record(self, max_s=15.0, start_timeout=4.0, tail_silence=0.8) -> np.ndarray:
        """Record until you stop talking. Silero VAD decides what's speech (not loudness), checked every 0.25 s."""
        chunks, t0, started = [], time.time(), False
        while time.time() - t0 < max_s:
            try:
                chunks.append(np.frombuffer(self.q.get(timeout=2), dtype=np.int16))
            except queue.Empty:
                break
            if len(chunks) % 2:
                continue
            audio = np.concatenate(chunks)
            started, finished = hearing.finished_speaking(audio, tail_silence)
            if finished:
                return audio
            if not started and time.time() - t0 > start_timeout:
                return np.array([], dtype=np.int16)
        return np.concatenate(chunks) if started and chunks else np.array([], dtype=np.int16)

    @property
    def whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel(config.load().get("whisper_model", "base.en"), device="cpu",
                                         compute_type="int8", download_root=str(WHISPER_DIR))
        self._whisper_used = time.time()
        return self._whisper

    def maybe_unload(self):
        hearing.unload_if_idle()
        if self._whisper is not None and time.time() - self._whisper_used > 600:
            import gc
            self._whisper = None
            gc.collect()

    def transcribe(self, audio: np.ndarray) -> str:
        """noise removal -> trim to speech -> whisper turbo on the GPU (falls back to the small CPU model)."""
        if audio.size < RATE * 0.3:
            return ""
        audio = hearing.trim(hearing.denoise(audio))
        text = hearing.transcribe_turbo(audio, vocabulary())
        if text is None:
            segs, _ = self.whisper.transcribe(audio.astype(np.float32) / 32768.0, language="en", beam_size=1,
                                              vad_filter=True, initial_prompt=vocabulary())
            text = " ".join(s.text.strip() for s in segs).strip()
        if re.fullmatch(r"\W*(thank you|thanks for watching|you)\W*", text, re.I) or not re.search(r"[A-Za-z]{2,}", text):
            text = ""  # whisper's hallucinations on near-silence, or no real words ("!", "...")
        text = correct(text)
        return re.sub(rf"^\W*(hey\W+)?{re.escape(self.phrase.split()[-1])}\W*", "", text, flags=re.I).strip()

    # ---------- server ----------
    def speak(self, text: str, wait=False):
        self.said = (getattr(self, "said", "") + " " + text).strip()
        try:
            requests.post(f"{self.base}/api/speak", json={"text": text}, headers=config.api_headers(), timeout=60)
        except requests.RequestException:
            return
        if wait:
            time.sleep(0.3)
            while self.speaking():
                time.sleep(0.2)

    def speaking(self) -> bool:
        try:
            return requests.get(f"{self.base}/api/speaking", headers=config.api_headers(), timeout=2).json().get("speaking", False)
        except requests.RequestException:
            return False

    def post(self, path, body=None):
        try:
            return requests.post(f"{self.base}{path}", json=body or {}, headers=config.api_headers(), timeout=60).json()
        except requests.RequestException as e:
            return {"ok": False, "error": str(e)}

    # ---------- one command ----------
    def handle(self, ptt: bool = False):
        if self.speaking():
            self.post("/api/stop")  # "Hey Arthur" / the talk key interrupts him
        subprocess.Popen(["/usr/bin/afplay", CHIME])
        hearing.preload_turbo()  # loads on the GPU while you're still talking
        threading.Thread(target=lambda: self.post("/api/warm"), daemon=True).start()  # and the voice, in parallel
        state("listening")
        if ptt:
            self._drain()
            heard = self.transcribe(self.record_held())
        else:
            time.sleep(0.15)
            heard = self.transcribe(self.record())
        # a fresh "Hey Arthur" within 2 minutes picks the conversation back up
        history = self.history if time.time() - getattr(self, "history_at", 0) < 120 else []
        followup = False
        while heard and len(history) < 20:
            act, said = self.turn(heard, history, followup)
            if act == "ignore":
                log.info("ignored (not addressed to Arthur): %s", heard)
                break
            history.append({"you": heard, "arthur": said, "action": act})
            log_turn(heard, said, act)
            self.history, self.history_at = history, time.time()
            if act == "stop" or ENDERS.search(heard):
                break
            # conversation mode: keep listening for a follow-up, no wake word needed
            self.wait_quiet()
            window = 20.0 if act == "hold" else 10.0 if said.rstrip().endswith("?") else 7.0
            subprocess.Popen(["/usr/bin/afplay", "-v", "0.25", CUE])
            state("listening", "", said)
            log.info("follow-up window %.0fs", window)
            audio = self.record(start_timeout=window)
            heard = self.transcribe(audio) if self.voice_ok(audio, "follow-up") else ""
            followup = True
            if heard:
                log.info("follow-up: %s", heard)
        state("idle")

    def rescan(self):
        """Re-read every channel, wait for the fresh briefing, then say what's new since the last one."""
        before = config.read_json(config.DATA / "briefing.json", {}) or {}
        old_ids = set((before.get("index") or {}).keys())
        r = self.post("/api/refresh")
        if not r.get("started") and r.get("reason") != "already running":
            return self.speak("I couldn't start a rescan, sir.")
        self.speak("Checking everything again, sir. About a minute.")
        state("thinking", "rescan")
        t0, last = time.time(), before.get("generated_at")
        while time.time() - t0 < 240:
            time.sleep(2)
            now = config.read_json(config.DATA / "briefing.json", {}) or {}
            if now.get("generated_at") and now.get("generated_at") != last:
                break
        else:
            return self.speak("The rescan is taking longer than usual, sir. The dashboard will update when it's done.")
        idx = now.get("index") or {}
        new = [v for k, v in idx.items() if k not in old_ids and v.get("source") != "calendar"]
        down = [k for k, v in (now.get("sources") or {}).items() if v.get("status") != "ok"]
        names = {"gmail": "email", "slack": "Slack message", "whatsapp": "WhatsApp chat", "imessage": "text"}
        if new:
            by = {}
            for v in new:
                by.setdefault(v["source"], []).append(re.sub(r"<.*?>|\(.*?\)|[\u202a-\u202e]", "", v.get("from") or "").strip(' "'))
            parts = [f"{len(w)} new {names.get(s, s)}{'s' if len(w) > 1 else ''}, from {', '.join(w[:3])}" for s, w in by.items()]
            msg = "All updated, sir. " + "; ".join(parts) + "."
        else:
            msg = "All updated, sir. Nothing new since the last check."
        top = (now.get("brief") or {}).get("headline")
        if top:
            msg += f" {top}"
        if down:
            msg += f" I couldn't reach {', '.join(down)}."
        self.speak(msg)

    def voice_ok(self, audio, what: str) -> bool:
        """True if this audio sounds like you (voiceprint). Push-to-talk never goes through here."""
        if audio is None or getattr(audio, "size", 0) == 0 or getattr(self, "typed", False):
            return True
        try:
            s = voiceprint.score(audio)
        except Exception as e:  # noqa: BLE001 — a broken voiceprint must not lock you out
            log.warning("voiceprint error: %s", e)
            return True
        cfg = config.load()
        # a short "yes" carries less voice than a full command, so approvals use a lower bar
        # (other voices saying "yes, send it" score ≤0.15; the owner's short clips mostly ≥0.4)
        bar = cfg.get("voiceprint_approval_threshold", 0.20) if what == "approval" else cfg.get("voiceprint_threshold", 0.30)
        ok = s is None or s >= bar or not cfg.get("voiceprint", True)
        log.info("voiceprint %s: %s (%.2f)", what, "you" if ok else "NOT you — ignored", s if s is not None else -1)
        return ok

    def wait_quiet(self, timeout=120):
        t = time.time()
        while self.speaking() and time.time() - t < timeout:
            time.sleep(0.15)
        time.sleep(0.25)
        self._drain()  # drop the tail of his own voice

    def turn(self, heard: str, history: list, followup: bool = False) -> tuple[str, str]:
        """One exchange: decide + act. Returns (action, what Arthur said)."""
        self.said = ""
        log.info("heard: %s", heard)
        state("thinking", heard)
        import concurrent.futures as cf
        streamer = Streamer(self)
        ex = cf.ThreadPoolExecutor(2)
        fut = ex.submit(commands.decide, heard, streamer.feed, history, followup)
        try:
            d = fut.result(timeout=3.0)
        except cf.TimeoutError:
            if not streamer.started:  # filler: spoken, but not part of his answer in the transcript
                self.post("/api/speak", {"text": "One moment, sir."})
            try:
                d = fut.result(timeout=6.0)
            except cf.TimeoutError:
                # hedge: Claude is having a slow moment — ask again in parallel and take whichever answers first
                log.info("decide slow (>9s), hedging with a second request")
                hedge = ex.submit(commands.decide, heard, None, history, followup)
                done_, _ = cf.wait([fut, hedge], return_when=cf.FIRST_COMPLETED, timeout=80)
                d = (done_.pop() if done_ else fut).result()
        ex.shutdown(wait=False, cancel_futures=True)
        streamer.close(d)
        log.info("decided: %s", {k: d.get(k) for k in ("action", "source_ids")})
        act, speech = d.get("action"), d.get("speech", "")
        state("speaking", heard, speech)

        if act in ("ignore", "hold"):
            if act == "hold" and speech:
                self.speak(speech)
            return act, speech
        if act in ("remember", "forget", "reminder_add", "timer_set", "music"):
            args = {"remember": {"fact": d.get("fact", "")}, "forget": {"query": d.get("query", "")},
                    "reminder_add": {"title": d.get("title", ""), "when": d.get("when")},
                    "timer_set": {"seconds": d.get("seconds") or 0, "label": d.get("label", "")},
                    "music": {"command": d.get("music_command", ""), "query": d.get("query", ""), "level": d.get("level")}}[act]
            r = self.post(f"/api/do/{act}", {"args": args})
            if not r.get("ok"):
                self.speak(f"That didn't work, sir: {r.get('error', 'unknown error')}.")
            elif act == "music" and r.get("playing"):
                self.speak(f"That's {r['playing']}.")
            elif act == "forget":
                self.speak(f"Done. I've forgotten that {r.get('forgot', 'it')}.")
            elif act == "timer_set":
                self.speak(speech or f"Timer set, sir. It ends at {r.get('ends')}.")
            elif act != "music" or speech:
                self.speak(speech or "Done, sir.")
            return act, self.said or speech
        if act == "brief":
            self.post("/api/speak", {})
        elif act == "stop":
            self.post("/api/stop", {})
            if speech and ENDERS.search(heard):  # "that's all / thanks" gets a brief sign-off; plain "stop" doesn't
                self.speak(speech)
        elif act == "rescan":
            self.rescan()
        elif act == "open_hud":
            from .__main__ import open_hud
            open_hud(config.load())
            self.speak(speech)
        elif act == "mark_done":
            self.post("/api/handled", {"ids": d.get("source_ids", [])})
            self.speak(speech or "Noted, sir.")
        elif act == "drive_find" and d.get("query"):
            r = self.post("/api/drive/search", {"query": d["query"], "open": bool(d.get("open"))})
            files = r.get("files") or []
            if not r.get("ok"):
                self.speak(f"Drive search failed: {r.get('error')}.")
            elif not files:
                self.speak(f"Nothing in your Drive matches {d['query']}, sir.")
            else:
                top = files[0]["name"]
                more = f" I found {len(files)} matches; the most recent is {top}." if len(files) > 1 else f" Found {top}."
                self.speak(more.strip() + (" Opening it now." if d.get("open") else " Shall I open it? Just ask."))
        elif act in commands.NEEDS_YES:
            self.confirm_and_run(d, heard)
        elif not streamer.started:
            self.speak(speech)
        if streamer.started:
            self.said = speech
        return act, self.said or speech

    def ask_yes(self, heard: str, what: str) -> str:
        """Listen (no wake word) for a short answer. Returns 'yes', 'draft' or 'no'.
        Typed commands are approved with the dashboard's buttons instead."""
        if getattr(self, "typed", False):
            return self.ask_on_screen(heard, what)
        for attempt in range(2):
            self._drain()
            subprocess.Popen(["/usr/bin/afplay", CHIME])
            state("confirm", heard, what)
            audio = self.record(max_s=6, start_timeout=5)
            if self.voice_ok(audio, "approval"):
                break
            if attempt == 0:  # say so, rather than quietly treating it as a no
                self.speak("Sorry sir, I didn't recognise your voice. Say yes once more.", wait=True)
        else:
            return "rejected"  # someone else can't approve sending on your behalf
        answer = self.transcribe(audio)
        log.info("confirm: %s", answer)
        return classify_answer(answer)

    def ask_on_screen(self, heard: str, what: str, timeout: float = 120) -> str:
        ANSWER.unlink(missing_ok=True)
        config.write_json(CONFIRM, {"heard": heard, "what": what, "question": getattr(self, "said", ""),
                                    "draft": getattr(self, "pending_action", "") in ("email_reply", "email_new"),
                                    "at": time.time()})
        state("confirm", heard, what)
        t0 = time.time()
        try:
            while time.time() - t0 < timeout:
                a = config.read_json(ANSWER)
                if a and a.get("answer") in ("yes", "draft", "no"):
                    log.info("confirm (on screen): %s", a["answer"])
                    return a["answer"]
                time.sleep(0.25)
            return "no"
        finally:
            CONFIRM.unlink(missing_ok=True)
            ANSWER.unlink(missing_ok=True)

    def handle_text(self, text: str):
        """A typed command from the dashboard: same brain and actions, approvals by button."""
        self.typed = True
        try:
            history = self.history if time.time() - getattr(self, "history_at", 0) < 120 else []
            act, said = self.turn(text, history, False)
            if act != "ignore":
                history.append({"you": text, "arthur": said, "action": act})
                log_turn(text, said, act)
                self.history, self.history_at = history, time.time()
        except Exception:  # noqa: BLE001
            log.exception("typed command failed")
        finally:
            self.typed = False
            state("idle" if time.time() < self.active_until else "dormant")

    def take_typed(self) -> bool:
        a = config.read_json(ASK)
        if not a:
            return False
        ASK.unlink(missing_ok=True)
        if a.get("text", "").strip():
            log.info("typed: %s", a["text"])
            self.handle_text(a["text"].strip())
        return True

    def confirm_and_run(self, d: dict, heard: str):
        act, ids = d["action"], d.get("source_ids") or []
        self.pending_action = act
        if act in ("slack_reply", "email_reply", "imessage_reply") and not ids:
            return self.speak("Which message should I reply to, sir?")
        if act in ("slack_reply", "email_reply", "imessage_reply") and not d.get("reply_text"):
            return self.speak("What would you like me to say?")
        if act in ("event_create", "event_move") and not d.get("when"):
            return self.speak("When should it be, sir?")
        if act == "event_move" and not d.get("event_id"):
            return self.speak("Which event should I move?")
        if act in ("claude_session", "claude_task") and not d.get("prompt"):
            return self.speak("What should Claude do, sir?")
        if act in ("call", "imessage_new", "email_new"):
            if not d.get("to"):
                return self.speak("Who should I contact?")
            if act != "call" and not d.get("reply_text"):
                return self.speak("What would you like me to say?")
            r = self.post(f"/api/do/{'resolve_email' if act == 'email_new' else 'resolve_phone'}", {"args": {"to": d["to"]}})
            if r.get("ambiguous"):
                names = r["ambiguous"]
                return self.speak(f"I have {len(names)} matches: " + ", ".join(names[:-1]) + f", and {names[-1]}. Which one?")
            if not r.get("ok"):
                return self.speak(f"I couldn't find them, sir: {r.get('error')}.")
            who = r.get("name") or d["to"]
            d = {**d, "to": r.get("email") or who}
            if who.lower() not in d.get("speech", "").lower():
                d["speech"] = f"That's {who}. " + d.get("speech", "")
        if act == "whatsapp_send":
            if not d.get("reply_text"):
                return self.speak("What would you like me to say?")
            t = self.post("/api/whatsapp/resolve", {"text": d["reply_text"], "id": (ids or [None])[0], "to": d.get("to")})
            if t.get("ambiguous"):
                names = t["ambiguous"]
                return self.speak(f"I have {len(names)} people by that name: " + ", ".join(names[:-1])
                                  + f", and {names[-1]}. Which one?")
            if t.get("group"):
                self.post("/api/whatsapp/compose", {"id": ids[0], "text": d["reply_text"]})
                return self.speak(f"WhatsApp won't let me message {t.get('name')} directly, sir. "
                                  "The text is on your clipboard; open the group and paste.")
            if not t.get("ok"):
                return self.speak(f"I couldn't find them, sir: {t.get('error')}.")
            d = {**d, "speech": f"That's {t['name']}. " + d.get("speech", "")} if t["name"].lower() not in d.get("speech", "").lower() else d
        self.speak(d.get("speech", ""), wait=True)
        what = d.get("reply_text") or d.get("prompt") or ""
        ans = self.ask_yes(heard, what)
        if act == "email_new" and ans in ("yes", "draft"):
            r = self.post("/api/do/email_new", {"args": {"to": d["to"], "subject": d.get("subject", ""),
                                                          "body": d["reply_text"], "send": ans == "yes"}})
            return self.speak(("Sent, sir." if ans == "yes" else "It's in your drafts, sir.") if r.get("ok")
                              else f"That didn't go through: {r.get('error')}.")
        if act == "email_reply" and ans == "draft":
            r = self.post("/api/email/reply", {"id": ids[0], "text": d["reply_text"], "send": False})
            return self.speak("It's in your drafts, sir." if r.get("ok") else f"Couldn't save it: {r.get('error')}.")
        if ans == "rejected":
            return self.speak("I still couldn't confirm it was you, so I haven't sent it. You can type the request "
                              "on the dashboard and approve it there.")
        if ans != "yes":
            return self.speak("Very well, I'll leave it.")
        if act == "slack_reply":
            r = self.post("/api/slack/send", {"id": ids[0], "text": d["reply_text"]})
        elif act == "email_reply":
            r = self.post("/api/email/reply", {"id": ids[0], "text": d["reply_text"], "send": True})
        elif act == "whatsapp_send":
            r = self.post("/api/whatsapp/send", {"id": (ids or [None])[0], "to": d.get("to"), "text": d["reply_text"]})
            if r.get("ok") and r.get("verified"):
                return self.speak("Sent, sir.")
            if r.get("ok"):
                return self.speak("I've pressed send, sir, but couldn't confirm it went through. Worth a glance at WhatsApp.")
            if r.get("typed") and "Accessibility" in str(r.get("error")):
                return self.speak("It's typed in WhatsApp, sir, but I need Accessibility permission to press send. "
                                  "Just press enter for now.")
            if r.get("typed"):
                return self.speak(f"It's typed in WhatsApp but I didn't press send, sir: {r.get('error')}.")
        elif act == "call":
            r = self.post("/api/do/call", {"args": {"to": d["to"]}})
            return self.speak("Ringing now, sir." if r.get("ok") else f"I couldn't place the call: {r.get('error')}.")
        elif act == "imessage_new":
            r = self.post("/api/do/imessage_new", {"args": {"to": d["to"], "text": d["reply_text"]}})
        elif act == "event_create":
            r = self.post("/api/do/event_create", {"args": {"title": d.get("title", ""), "start": d.get("when"),
                          "minutes": d.get("minutes"), "attendees": d.get("attendees") or [], "location": d.get("location", "")}})
            if r.get("ok"):
                extra = f" Couldn't find an email for {', '.join(r['not_found'])}." if r.get("not_found") else ""
                return self.speak(("Added and invitations sent, sir." if r.get("invited") else "Added to your calendar, sir.") + extra)
        elif act == "event_move":
            r = self.post("/api/do/event_move", {"args": {"event_id": d.get("event_id", ""), "start": d.get("when"),
                          "minutes": d.get("minutes")}})
            if r.get("ok"):
                return self.speak("Moved, sir.")
        elif act == "imessage_reply":
            r = self.post("/api/imessage/send", {"id": ids[0], "text": d["reply_text"]})
        elif act == "claude_session":
            r = self.post("/api/claude/session", {"project": d["project"], "prompt": d["prompt"]})
            return self.speak("Session's open in Terminal, sir." if r.get("ok") else f"Couldn't open it: {r.get('error')}.")
        elif act == "claude_task":
            r = self.post("/api/claude/task", {"project": d["project"], "prompt": d["prompt"]})
            return self.speak("On it. I'll let you know when it's done." if r.get("ok") else f"Couldn't start it: {r.get('error')}.")
        else:
            r = {"ok": False, "error": "unknown action"}
        self.speak("Sent, sir." if r.get("ok") else f"That didn't go through: {r.get('error', 'unknown error')}.")

    def feed(self, data: bytes) -> bool:
        """One 125 ms block in; True if the wake phrase was just heard. Skips decoding during silence."""
        b = np.frombuffer(data, dtype=np.int16)
        self.ring.append(b)
        self._debug_capture(b)
        rms = float(np.sqrt(np.mean(b.astype(np.float32) ** 2))) if b.size else 0.0
        self.noise = 0.98 * self.noise + 0.02 * min(rms, 3000)
        was_hot = time.time() <= getattr(self, "hot_until", 0)
        if rms > self.noise * 1.3 + 40:
            self.hot_until = time.time() + 1.5
        if time.time() > getattr(self, "hot_until", 0):
            if time.time() - getattr(self, "last_gc", 0) > 60:
                self.last_gc = time.time()
                self.maybe_unload()
            return False
        if not was_hot:
            # speech just started: the gate opened on this block, so replay the last ~0.5 s first —
            # otherwise the quiet start of "hey" is skipped and the wake phrase is never seen.
            self.rec = self.Rec(self.vosk, RATE, self.grammar)
            for pre in list(self.ring)[-5:-1]:
                self.rec.AcceptWaveform(pre.tobytes())
        # Vosk is only a cheap gate: its small model can't tell "arthur" from "author", so any sound-alike is a
        # candidate and whisper (verify) makes the call.
        if getattr(self, "pending", 0):  # candidate heard: collect a little more audio so "Arthur" isn't clipped
            self.pending -= 1
            return self.pending == 0 and self.verify()
        if self.rec.AcceptWaveform(data):
            text = json.loads(self.rec.Result()).get("text", "")
            if re.search(r"\b(hey|arthur|author|other)\b", text) and not CANDIDATE.search(text):
                log.info("near-miss (vosk heard): %r", text)  # diagnostics: what a missed wake sounded like
        else:
            text = json.loads(self.rec.PartialResult()).get("partial", "")
        if CANDIDATE.search(text):
            if not config.load().get("wake_verify", False):
                return True
            self.pending = 3  # ~0.4 s, then whisper double-checks
        return False

    def _debug_capture(self, b):
        """touch data/debug_record -> the next 6 s of mic audio is saved to data/debug.wav (diagnostics only)."""
        flag = config.DATA / "debug_record"
        if not flag.exists() and not getattr(self, "_dbg", None):
            return
        self._dbg = getattr(self, "_dbg", None) or []
        self._dbg.append(b.copy())
        flag.unlink(missing_ok=True)
        if len(self._dbg) * BLOCK >= RATE * 6:
            import soundfile as sf
            sf.write(config.DATA / "debug.wav", np.concatenate(self._dbg), RATE)
            self._dbg = None

    def verify(self) -> bool:
        """Second opinion: whisper must hear the actual name in the last few seconds."""
        audio = np.concatenate(list(self.ring)) if self.ring else np.array([], dtype=np.int16)
        if audio.size < RATE * 0.4:
            return False
        # no prompt here on purpose: priming with "Arthur" makes whisper hear it in "author"/"Archer"
        segs, _ = self.whisper.transcribe(audio.astype(np.float32) / 32768.0, language="en", beam_size=5)
        text = " ".join(x.text for x in segs).lower()
        # "Arthur" and "author" are near-homophones in many accents; requiring "hey" right before keeps
        # "talk to Arthur later" / "hey Archer" out. A real "hey author" in your room is the accepted cost.
        ok = bool(VERIFY.search(text))
        self.rec = self.Rec(self.vosk, RATE, self.grammar)
        if not ok:
            log.info("wake rejected: %r", text.strip()[:60])
        return ok

    # ---------- main loop ----------
    def pick_mic(self):
        """Prefer the built-in mic: holding a Bluetooth headset mic open drops the headphones into call-quality audio."""
        want = config.load().get("mic", "MacBook Pro Microphone")
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and want.lower() in d["name"].lower():
                return i, d["name"]
        d = sd.query_devices(kind="input")
        return None, d["name"]

    # ---------- power saving: the mic is only open while Arthur is "awake" ----------
    def active_seconds(self) -> float:
        return float(config.load().get("active_minutes", 10)) * 60

    def wake_requested(self) -> bool:
        """Hold right ⌥, the menu's "Listen for 10 minutes", or the morning/evening briefing wakes him."""
        if self.ptt_pressed and LOCKED.exists():
            self.ptt_pressed = False  # not while the Mac is locked
        if self.ptt_pressed:
            return True
        if WAKE.exists():
            WAKE.unlink(missing_ok=True)
            return True
        return False

    def run(self):
        always = config.load().get("always_listen", False)
        self.active_until = 0.0
        while True:
            if not always and time.time() > self.active_until:
                AWAKE["until"] = 0.0
                state("dormant")
                log.info("sleeping: mic closed until right ⌥ / Listen")
                while not self.wake_requested():  # mic closed: no listening, no battery drain
                    self.take_typed()  # typed commands work even while he's asleep
                    time.sleep(0.2)
                self.active_until = time.time() + self.active_seconds()
                AWAKE["until"] = self.active_until
                state("idle")
            self.listen_session(always)
            sd._terminate()
            sd._initialize()

    def listen_session(self, always: bool):
        """Mic open. Returns when the active window lapses (or the mic changes / errors)."""
        dev, name = self.pick_mic()
        log.info("listening for '%s' on mic: %s (awake until %s)", self.phrase, name,
                 "always" if always else time.strftime("%H:%M", time.localtime(self.active_until)))
        try:
            with sd.RawInputStream(samplerate=RATE, blocksize=BLOCK, dtype="int16", channels=1,
                                   callback=self._cb, device=dev):
                last_check = time.time()
                while always or time.time() < self.active_until:
                    try:
                        data = self.q.get(timeout=2)
                    except queue.Empty:
                        raise RuntimeError("mic stopped delivering audio")
                    if WAKE.exists():  # "Listen for 10 minutes" while already awake: extend
                        WAKE.unlink(missing_ok=True)
                        self.active_until = time.time() + self.active_seconds()
                        AWAKE["until"] = self.active_until
                        state("idle")
                    if self.ptt_pressed:
                        self.ptt_pressed = False
                        if not LOCKED.exists():
                            self.run_command(ptt=True)
                        continue
                    if ASK.exists():
                        self.take_typed()
                        self._drain()
                        continue
                    if PAUSE.exists() or LOCKED.exists():  # paused by you / training / Mac locked
                        continue
                    hit = self.feed(data)
                    if hit and not self.voice_ok(np.concatenate(list(self.ring)) if self.ring else None, "wake"):
                        self.rec = self.Rec(self.vosk, RATE, self.grammar)  # not you: forget the half-heard wake
                        hit = False
                    if hit:
                        self.rec = self.Rec(self.vosk, RATE, self.grammar)
                        self.run_command(ptt=False)
                    if time.time() - last_check > 10:
                        last_check = time.time()
                        if self.pick_mic()[1] != name:
                            log.info("mic changed, re-opening")
                            return
        except Exception as e:  # noqa: BLE001
            log.warning("mic error (%s), retrying in 3s", e)
            time.sleep(3)

    def run_command(self, ptt: bool):
        try:
            self.handle(ptt=ptt)
        except Exception:  # noqa: BLE001 — never let one bad command kill the listener
            log.exception("command failed")
            state("idle")
        self._drain()
        self.active_until = time.time() + self.active_seconds()  # every use keeps him awake another 10 min
        AWAKE["until"] = self.active_until
        state("idle")

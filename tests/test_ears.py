"""Ears test, fully offline, no mic: synthesise speech, run it through the exact wake-word + transcription code.

1. Wake word: positives ("hey Arthur ...") must trigger, look-alikes and Arthur's own voice must not.
2. Transcription: command clips -> whisper -> word accuracy.
3. Confirm classifier: yes / draft / no phrasings.
Run:  .venv/bin/python tests/test_ears.py
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arthur import ears  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="arthur-test-"))
SAY_VOICES = ["Daniel", "Aman", "Samantha", "Rishi", "Karen", "Moira"]
KOKORO_VOICES = ["am_michael", "bm_george", "af_heart", "bf_emma"]


def say_clip(text, voice, i):
    out = TMP / f"say{i}.wav"
    r = subprocess.run(["say", "-v", voice, "-o", str(out), "--file-format=WAVE", "--data-format=LEI16@16000", text],
                       capture_output=True)
    return None if r.returncode else out


def kokoro_clip(text, voice, i):
    from arthur.voice import _engine
    s, sr = _engine().create(text, voice=voice, speed=1.0, lang="en-us" if voice.startswith("a") else "en-gb")
    raw, out = TMP / f"k{i}_raw.wav", TMP / f"k{i}.wav"
    sf.write(raw, s, sr)
    subprocess.run(["/opt/homebrew/bin/ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(raw), "-ar", "16000",
                    "-ac", "1", "-sample_fmt", "s16", str(out)], check=True)
    return out


def load16(path, noise=0.0):
    a, sr = sf.read(path, dtype="int16")
    if a.ndim > 1:
        a = a[:, 0]
    pad = np.zeros(8000, dtype=np.int16)
    a = np.concatenate([pad, a, pad, pad])
    if noise:  # room hiss
        a = np.clip(a + np.random.normal(0, noise, a.size), -32768, 32767).astype(np.int16)
    return a


def wake_hits(e, audio):
    e.rec = e.Rec(e.vosk, ears.RATE, e.grammar)
    e.noise, e.hot_until, e.pending = 300.0, 0, 0
    e.ring.clear()
    for i in range(0, len(audio) - ears.BLOCK, ears.BLOCK):
        if e.feed(audio[i:i + ears.BLOCK].tobytes()):
            return True
    return False


def wer(ref, hyp):
    import re
    r, h = re.findall(r"[a-z']+", ref.lower()), re.findall(r"[a-z']+", hyp.lower())
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=int)
    d[:, 0], d[0, :] = range(len(r) + 1), range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + (r[i - 1] != h[j - 1]))
    return d[-1, -1] / max(len(r), 1)


POSITIVE = ["Hey Arthur", "Hey Arthur, what's the weather?", "Hey Arthur, read me the briefing.",
            "Okay, hey Arthur, who's waiting on me?"]
NEGATIVE = ["Hey author, did you finish the book?", "Arthur's car is parked outside.", "My heart rate is up.",
            "Hey Siri, set a timer.", "Hey Jarvis.", "Hey Archer, nice shot.", "I'll talk to Arthur later.",
            "Good morning, sir. It's half past eight on Sunday. Clear skies and twenty eight degrees.",
            "Hey, are you there?", "Hey other team, over here."]
COMMANDS = ["what's on my calendar tomorrow", "reply to Happy on WhatsApp saying I'll do the DNS tonight",
            "open Claude in the stark dashboard and fix the date filter", "find the Malibu photoshoot folder",
            "mark the Stark Expo thing as done", "draft an email to Hammer Industries saying got it"]
CONFIRM = {"yes": "yes", "yeah send it": "yes", "yes please": "yes", "go ahead": "yes", "sure": "yes",
           "send it": "yes", "no": "no", "no don't": "no", "wait": "no", "cancel that": "no", "not yet": "no",
           "keep it as a draft": "draft", "draft it": "draft", "hmm": "no", "": "no",
           "don't send it": "no", "yes but wait": "no", "never mind": "no", "do not send": "no", "draft it, don't send it": "draft", "just save it as a draft": "draft",
           "don't draft it": "no", "no, draft it": "draft"}


def main():
    e = ears.Ears()
    available = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    say_voices = [v for v in SAY_VOICES if any(line.startswith(v + " ") for line in available.splitlines())]
    rows, i = [], 0

    def clips(text):
        nonlocal i
        for v in say_voices:
            i += 1
            p = say_clip(text, v, i)
            if p:
                yield f"say:{v}", p
        for v in KOKORO_VOICES:
            i += 1
            yield f"kokoro:{v}", kokoro_clip(text, v, i)

    # 1. wake word
    tp = fn = fp = tn = 0
    misses, false_alarms = [], []
    for text in POSITIVE:
        for voice, p in clips(text):
            for noise in (0, 250):
                hit = wake_hits(e, load16(p, noise))
                tp += hit
                fn += not hit
                if not hit:
                    misses.append(f"{voice}{' +noise' if noise else ''}: {text}")
    for text in NEGATIVE:
        for voice, p in clips(text):
            hit = wake_hits(e, load16(p))
            fp += hit
            tn += not hit
            if hit:
                false_alarms.append(f"{voice}: {text}")
    # Arthur's real briefing audio must never wake him
    briefing_wavs = sorted(Path(ears.config.DATA / "voice").glob("*.wav"))[-3:]
    self_trigger = []
    for p in briefing_wavs:
        a, sr = sf.read(p, dtype="int16")
        a = a[:, 0] if a.ndim > 1 else a
        if sr != 16000:
            subprocess.run(["/opt/homebrew/bin/ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(p), "-ar", "16000",
                            "-ac", "1", "-sample_fmt", "s16", str(TMP / "self.wav")], check=True)
            a = load16(TMP / "self.wav")
        if wake_hits(e, a):
            self_trigger.append(p.name)

    # 2. transcription
    t_rows = []
    for text in COMMANDS:
        for voice, p in clips(text):
            t0 = time.time()
            hyp = e.transcribe(load16(p, 150))
            t_rows.append({"voice": voice, "ref": text, "hyp": hyp, "wer": round(wer(text, hyp), 2),
                           "secs": round(time.time() - t0, 2)})

    # 3. confirm classifier
    c_fail = [(k, ears.classify_answer(k), v) for k, v in CONFIRM.items() if ears.classify_answer(k) != v]

    report = {
        "wake": {"recall": f"{tp}/{tp + fn}", "false_alarms": f"{fp}/{fp + tn}", "misses": misses,
                 "false_alarm_cases": false_alarms, "self_trigger_on_own_voice": self_trigger},
        "transcribe": {"mean_wer": round(float(np.mean([r["wer"] for r in t_rows])), 3),
                       "worst": sorted(t_rows, key=lambda r: -r["wer"])[:6],
                       "mean_secs": round(float(np.mean([r["secs"] for r in t_rows])), 2)},
        "confirm": {"passed": f"{len(CONFIRM) - len(c_fail)}/{len(CONFIRM)}", "failures": c_fail},
        "voices": say_voices + KOKORO_VOICES,
    }
    print(json.dumps(report, indent=2))
    (Path(__file__).parent / "results_ears.json").write_text(json.dumps({**report, "all_transcripts": t_rows}, indent=2))


if __name__ == "__main__":
    main()

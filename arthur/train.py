"""Voice training:  .venv/bin/python -m arthur train   (run in Terminal, ~6 minutes)

Part 1 (wake word) and part 2 (reading ~25 sentences full of your names and usual commands).
Part 2 measures which words get misheard in your voice and saves auto-corrections for made-up words
(names like Malibu), and compares the bigger free whisper model on your voice.

Records you saying "Hey Arthur" 20 times plus a few look-alike sentences, then tunes detection to what the
models actually hear in your voice: extra Vosk candidates and extra whisper spellings get saved to config.json,
but only ones that never show up in your look-alike sentences. Prints recall / false alarms before and after.
The live listener is paused while this runs.
"""
import json
import re
import subprocess
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

from . import config, ears

OUT = config.DATA / "wake_samples"
CHIME = "/System/Library/Sounds/Tink.aiff"
READING = [
    "Hey Arthur, what's on my calendar tomorrow?",
    "Reply to Happy on WhatsApp saying I'll fix the DNS tonight.",
    "Find the Malibu photoshoot folder in Drive.",
    "Mark the Stark Expo follow-up as done.",
    "Draft an email to Hammer Industries saying the photos are coming Tuesday.",
    "Who is waiting on me from Avengers Tower and Damage Control?",
    "Tell Pepper on Slack that the invoice went out.",
    "Have Claude check why the stark dashboard sync failed.",
    "Open Claude in omniscrape and fix the Instagram scraper.",
    "Any messages from Rhodey or Peter today?",
    "Read me the briefing.",
    "What did Oscorp and Roxxon send overnight?",
    "Reply to Mum saying I'll pick up the shawarma.",
    "Check the Pym Tech and Wakanda Design leads from yesterday.",
    "Is the AIM report ready for Shield?",
    "Summarise the Hydra and Baxter emails.",
    "What's the weather in Bengaluru this evening?",
    "Send the Rand Corp proposal update to Pepper.",
    "Open the Higgsfield and Semrush receipts.",
    "Stop. Never mind. Check again.",
    "Have Claude update the Webflow page for Neo and Cerno.",
    "Who's waiting on a reply about GoDaddy and Meta?",
    "Remind me what Sokovia and Kamar-Taj were about.",
    "Yes, send it. No, keep it as a draft.",
]

LOOKALIKES = [
    "Hey author, did you finish the book?",
    "I'll talk to Arthur about it later.",
    "Hey other team, over here.",
    "Hey Archer, nice shot.",
    "Hey, are you there?",
    "The art is on the wall.",
]


class Mic:
    """Continuous mic stream; utterances are cut out by Silero VAD, so timing never depends on chimes."""

    def __init__(self, dev):
        import queue
        self.q = queue.Queue()
        self.stream = sd.InputStream(samplerate=ears.RATE, blocksize=ears.BLOCK, channels=1, dtype="int16",
                                     device=dev, callback=lambda d, f, t, s: self.q.put(d[:, 0].copy()))
        self.stream.start()

    def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    def utterance(self, timeout=15.0, tail=0.7) -> np.ndarray | None:
        """Wait for one phrase: returns it (with a little padding) once you've stopped talking."""
        from . import hearing
        buf, t0 = [], time.time()
        while time.time() - t0 < timeout:
            buf.append(self.q.get())
            if len(buf) % 2:
                continue
            a = np.concatenate(buf)
            spans = hearing.speech_spans(a, 350)
            if spans and (a.size - spans[-1]["end"]) / ears.RATE >= tail:
                return a[max(0, spans[0]["start"] - 4800): spans[-1]["end"] + 4800]
            if not spans and len(buf) > 24:
                buf = buf[-8:]  # keep the last second of silence only
        return None

    def close(self):
        self.stream.stop()
        self.stream.close()


def rec(seconds: float, dev) -> np.ndarray:
    a = sd.rec(int(seconds * ears.RATE), samplerate=ears.RATE, channels=1, dtype="int16", device=dev)
    sd.wait()
    return a[:, 0]


def analyse(e: ears.Ears, audio: np.ndarray) -> dict:
    """What each stage hears for one clip."""
    r = e.Rec(e.vosk, ears.RATE, e.grammar)
    texts = []
    for i in range(0, len(audio) - ears.BLOCK, ears.BLOCK):
        if r.AcceptWaveform(audio[i:i + ears.BLOCK].tobytes()):
            texts.append(json.loads(r.Result()).get("text", ""))
    texts.append(json.loads(r.FinalResult()).get("text", ""))
    segs, _ = e.whisper.transcribe(audio.astype(np.float32) / 32768.0, language="en", beam_size=5)
    return {"vosk": " ".join(t for t in texts if t).strip(), "whisper": " ".join(s.text for s in segs).strip().lower()}


def wer_align(ref: str, hyp: str):
    """Word error rate + (ref_word, hyp_word) pairs that were misheard. Pairs are only formed between
    similar-sounding words, so an alignment shift never teaches a bogus correction."""
    from difflib import SequenceMatcher

    r, h = re.findall(r"[a-z0-9']+", ref.lower()), re.findall(r"[a-z0-9']+", hyp.lower())
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    subs = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=r, b=h, autojunk=False).get_opcodes():
        if tag != "replace":
            continue
        for rw in r[i1:i2]:
            best = max(h[j1:j2], key=lambda hw: SequenceMatcher(a=rw, b=hw).ratio())
            if SequenceMatcher(a=rw, b=best).ratio() >= 0.6:
                subs.append((rw, best))
    return d[-1][-1] / max(len(r), 1), subs


def reading_session(e, mic):
    print("\nPART 2 — read each sentence when it appears, at your normal pace.\n")
    subprocess.run(["say", "-v", "Daniel", "Part two. Read each sentence when it appears."])
    clips = []
    for i, line in enumerate(READING):
        print(f"  {i + 1}/{len(READING)}  {line}")
        mic.drain()
        u = mic.utterance(timeout=25, tail=0.9)
        if u is None:
            print("      (missed — moving on)")
            u = np.zeros(ears.RATE, dtype=np.int16)
        sf.write(OUT / f"read_{i:02}.wav", u, ears.RATE)
        clips.append(u)
    return clips


def analyse_reading(e, clips):
    """Transcribe your reading with the small CPU model and with turbo (what Arthur uses now); learn corrections
    from turbo's mistakes."""
    from . import hearing

    english = {w.strip().lower() for w in open("/usr/share/dict/words")}
    prompt = ears.vocabulary()
    out = {}
    for name in ("small", "turbo"):
        errs, subs, t0 = [], [], time.time()
        for ref, a in zip(READING, clips):
            if name == "turbo":
                hyp = hearing.transcribe_turbo(a, prompt) or ""
            else:
                segs, _ = e.whisper.transcribe(a.astype(np.float32) / 32768.0, language="en", beam_size=1,
                                               vad_filter=True, initial_prompt=prompt)
                hyp = " ".join(x.text for x in segs)
            w, sb = wer_align(ref, hyp)
            errs.append(w)
            subs += sb
        out[name] = {"wer": float(np.mean(errs)), "secs": (time.time() - t0) / len(clips), "subs": subs}
    corr = dict(config.load().get("corrections", {}))
    casing = {w.lower(): w for v in config.load().get("vocabulary", []) for w in v.split()}
    for ref, hyp in out["turbo"]["subs"]:
        split = ref.startswith(hyp) or ref.endswith(hyp)  # "whatsapp" heard as "whats app": not a mishearing
        if hyp not in english and ref != hyp and len(hyp) > 3 and not split:
            corr[hyp] = casing.get(ref, ref.capitalize())
    return out, corr


def passes(h: dict) -> bool:
    return bool(ears.candidate_re().search(h["vosk"])) and bool(ears.verify_re().search(h["whisper"]))


def main():
    e = ears.Ears.__new__(ears.Ears)
    ears.Ears.__init__(e)  # loads models; no mic stream is opened by __init__
    dev, name = e.pick_mic()
    OUT.mkdir(parents=True, exist_ok=True)
    ears.PAUSE.touch()
    try:
        print(f"\nMic: {name}. The live listener is paused while this runs.\n")
        mic = Mic(dev)
        print('PART 1 — say  "Hey Arthur"  20 times at your own pace, with a short pause between each.\n')
        subprocess.run(["say", "-v", "Daniel", "Say hey Arthur, twenty times, at your own pace, with a short pause between each."])
        mic.drain()
        pos = []
        while len(pos) < 20:
            u = mic.utterance(timeout=20)
            if u is None:
                print("\n  (didn't hear anything — keep going, say: Hey Arthur)")
                continue
            if u.size > ears.RATE * 3.5:
                print("\n  (that was long — just the two words, then pause)")
                continue
            sf.write(OUT / f"pos_{len(pos):02}.wav", u, ears.RATE)
            pos.append(u)
            print(f"  caught {len(pos)}/20", end="\r", flush=True)
        print("\n\nNow read each sentence when it appears (these must NOT wake him):")
        subprocess.run(["say", "-v", "Daniel", "Good. Now read each sentence when it appears."])
        neg = []
        for i, line in enumerate(LOOKALIKES):
            print(f'  -> "{line}"')
            mic.drain()
            u = mic.utterance(timeout=20)
            if u is not None:
                sf.write(OUT / f"neg_{i:02}.wav", u, ears.RATE)
                neg.append(u)
        clips = reading_session(e, mic)
        mic.close()
    finally:
        ears.PAUSE.unlink(missing_ok=True)

    print("\nAnalysing...")
    ph = [analyse(e, a) for a in pos]
    nh = [analyse(e, a) for a in neg]
    before = (sum(passes(h) for h in ph), sum(passes(h) for h in nh))

    # learn what your "hey arthur" sounds like to each model, excluding anything seen in look-alikes
    neg_vosk = " | ".join(h["vosk"] for h in nh)
    neg_whisper = " | ".join(h["whisper"] for h in nh)
    from difflib import SequenceMatcher
    vosk_new, whisper_new = set(), set()
    like = lambda w: SequenceMatcher(a="arthur", b=w).ratio() >= 0.55  # noqa: E731
    for h in ph:
        m = re.search(r"\bhey (\w+)", h["vosk"])
        if m and like(m.group(1)) and not ears.candidate_re().search(h["vosk"]) and f"hey {m.group(1)}" not in neg_vosk:
            vosk_new.add(m.group(1))
        m = re.search(r"\b(?:hey|hi|okay)\W{1,3}(\w+)", h["whisper"])
        if m and like(m.group(1)) and not ears.verify_re().search(h["whisper"]) and m.group(1) not in neg_whisper:
            whisper_new.add(m.group(1))
    cfg = config.load()
    cfg["wake_vosk_extra"] = sorted(set(cfg.get("wake_vosk_extra", [])) | vosk_new)
    cfg["wake_whisper_extra"] = sorted(set(cfg.get("wake_whisper_extra", [])) | whisper_new)
    config.save(cfg)
    after = (sum(passes(h) for h in ph), sum(passes(h) for h in nh))

    print(f"\nRecall on your voice:   {before[0]}/20  ->  {after[0]}/20")
    print(f"Look-alikes that woke:  {before[1]}/{len(nh)}  ->  {after[1]}/{len(nh)}")
    print(f"Learned Vosk variants:  {sorted(vosk_new) or 'none needed'}")
    print(f"Learned spellings:      {sorted(whisper_new) or 'none needed'}")
    misses = [h for h in ph if not passes(h)]
    if misses:
        print("Still missed (vosk | whisper):", *[f"  {h['vosk']!r} | {h['whisper']!r}" for h in misses[:5]], sep="\n")
    print("\nPart 2 analysis (comparing speech models on your voice)...")
    res, corr = analyse_reading(e, clips)
    cfg = config.load()
    cfg["corrections"] = corr
    config.save(cfg)
    sm, tb = res["small"], res["turbo"]
    print(f"Word errors in your voice:  small model {sm['wer']:.0%} ({sm['secs']:.1f}s)   "
          f"turbo (in use) {tb['wer']:.0%} ({tb['secs']:.1f}s)")
    print(f"Auto-corrections saved:     {len(corr)}  e.g. {dict(list(corr.items())[:6])}")
    (OUT / "report.json").write_text(json.dumps({"small": sm, "turbo": tb, "corrections": corr}, indent=1, default=str))
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{__import__('os').getuid()}/com.arthur.ears"])
    print("\nListener restarted with your tuning. Samples saved in data/wake_samples/.")

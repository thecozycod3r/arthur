"""Speech-to-text comparison: small CPU model vs turbo GPU, denoise on/off, quiet vs recorded car noise.
Run:  PYTHONPATH=.:tests .venv/bin/python tests/test_stt.py [noise.wav]"""
import sys
import time

import numpy as np
import soundfile as sf

import test_ears as T
from arthur import ears, hearing

noise_path = sys.argv[1] if len(sys.argv) > 1 else "data/car_noise.wav"
e = ears.Ears.__new__(ears.Ears)
e._whisper, e._whisper_used = None, 0
noise, _ = sf.read(noise_path, dtype="int16")
PHRASES = ["find the Malibu photoshoot folder", "reply to Happy on WhatsApp saying I'll do the DNS tonight",
           "what's on my calendar tomorrow", "mark the Stark Expo follow up as done", "have Claude check the stark dashboard sync"]
clips = [(p, T.load16(T.say_clip(p, v, 8000 + i * 10 + j))) for i, p in enumerate(PHRASES) for j, v in enumerate(["Rishi", "Aman", "Daniel"])]
vocab = ears.vocabulary()


def mix(a, k):
    return np.clip(a + np.resize(noise, a.size).astype(np.float32) * k, -32768, 32767).astype(np.int16)


def small(x):
    return " ".join(s.text for s in e.whisper.transcribe(x.astype(np.float32) / 32768, language="en", beam_size=1,
                                                         initial_prompt=vocab)[0])


def turbo(x):
    r = hearing.transcribe_turbo(x, vocab)
    if r is None:
        raise RuntimeError("turbo unavailable")
    return r


small(clips[0][1])  # warm both
t = time.time(); turbo(clips[0][1]); print(f"turbo cold load + first run {time.time() - t:.1f}s")
for cond, k in [("quiet", 0), ("car noise", 1), ("car noise x2", 2)]:
    for name, fn in [("small CPU", small), ("turbo GPU", turbo)]:
        for dn in (False, True):
            w, ts = [], []
            for p, a in clips:
                x = mix(a, k) if k else a
                if dn:
                    x = hearing.denoise(x, force=True)
                t = time.time()
                h = fn(x)
                ts.append(time.time() - t)
                w.append(T.wer(p, h))
            print(f"{cond:<13} {name:<10} denoise={'on ' if dn else 'off'}  WER {np.mean(w):5.0%}   {np.mean(ts):.2f}s")

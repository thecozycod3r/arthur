"""JARVIS voice: Kokoro neural TTS (offline) → ffmpeg character chain → afplay.

Falls back to macOS `say` if the model isn't available.
"""
import hashlib
import subprocess
import wave
from pathlib import Path

from . import config

MODELS = config.ROOT / "models"
CACHE = config.DATA / "voice"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

# The JARVIS character: a touch lower, a faint synthetic shimmer, a small hard room.
FX = {
    "helmet": None,  # built by helmet(intensity), see voice_fx_intensity in config.json
    "arthur": ",".join([
        "asetrate=24000*0.965", "aresample=24000", "atempo=1/0.965",
        "highpass=f=110", "lowpass=f=9000",
        "equalizer=f=3000:t=q:w=1.2:g=3",
        "chorus=0.7:0.9:18|26:0.22|0.18:0.30|0.25:1.6|2.1",
        "aecho=0.85:0.55:28|46:0.16|0.09",
        "acompressor=threshold=-18dB:ratio=3:attack=5:release=80",
        "loudnorm=I=-16:TP=-1.5",
    ]),
    "clean": "highpass=f=70,acompressor=threshold=-20dB:ratio=2,loudnorm=I=-16:TP=-1.5",
}



def helmet(intensity: float = 1.0) -> str:
    """In-suit comms sound. intensity 1.0 = full strength; 0.8 = 80%."""
    k = max(0.0, min(1.0, intensity))
    pitch = 1 - 0.05 * k
    return ",".join([
        f"asetrate=24000*{pitch:.4f}", "aresample=24000", f"atempo=1/{pitch:.4f}",
        f"highpass=f={int(70 + 110 * k)}", f"lowpass=f={int(11000 - 4000 * k)}",
        f"equalizer=f=2500:t=q:w=1:g={5 * k:.2f}",
        f"flanger=delay=1.5:depth={1.2 * k:.2f}:regen=-20:width={int(60 * k)}:speed=0.25",
        "chorus=0.6:0.9:12|20|31:" + "|".join(f"{d * k:.3f}" for d in (0.30, 0.25, 0.2)) + ":"
        + "|".join(f"{d * k:.3f}" for d in (0.35, 0.30, 0.25)) + ":1.4|1.9|2.6",
        "aecho=0.8:0.6:18|33|52:" + "|".join(f"{d * k:.3f}" for d in (0.22, 0.14, 0.08)),
        "acompressor=threshold=-20dB:ratio=4:attack=3:release=60",
        "volume=2.2", "alimiter=limit=0.9",
    ])


_kokoro = None
_last_used = 0.0
IDLE_UNLOAD_S = 600


class _Worker:
    """Kokoro lives in a helper process. Killing it after 10 idle minutes returns the ~400 MB to macOS
    (an in-process unload doesn't: Python/ONNX keep the freed memory reserved)."""

    def __init__(self):
        import sys
        import threading
        self.p = subprocess.Popen([sys.executable, "-m", "arthur", "ttsd"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, cwd=str(config.ROOT), bufsize=1)
        self.lock = threading.Lock()

    def render(self, text: str) -> Path:
        import json as _j
        with self.lock:
            self.p.stdin.write(_j.dumps({"text": text}) + "\n")
            self.p.stdin.flush()
            line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("tts worker died")
        r = _j.loads(line)
        if "error" in r:
            raise RuntimeError(r["error"])
        return Path(r["path"])

    def alive(self) -> bool:
        return self.p.poll() is None

    def stop(self):
        if self.alive():
            self.p.kill()


_worker = None


def warm() -> None:
    """Start the voice helper ahead of time (called when you start talking to Arthur)."""
    global _worker, _last_used
    import time
    _last_used = time.time()
    if _worker is None or not _worker.alive():
        _worker = _Worker()


def render_via_worker(text: str) -> Path:
    """Used by the server: cached wavs return instantly; otherwise the helper process renders."""
    global _worker, _last_used
    import time
    _last_used = time.time()
    cached = _cache_path(text)
    if cached.exists():
        return cached
    if _worker is None or not _worker.alive():
        _worker = _Worker()
    return _worker.render(text)


def unload_if_idle():
    """Free the ~300 MB voice model after 10 idle minutes (called periodically by the server)."""
    global _kokoro
    import gc
    import time
    global _worker
    idle_s = float(config.load().get("voice_idle_minutes", IDLE_UNLOAD_S / 60)) * 60
    if _worker is not None and _worker.alive() and time.time() - _last_used > idle_s:
        _worker.stop()
        _worker = None
        import logging
        logging.getLogger("arthur").info("voice helper stopped after %.1f idle minutes (memory returned)", idle_s / 60)
    if _kokoro is not None and time.time() - _last_used > IDLE_UNLOAD_S:
        _kokoro = None
        gc.collect()
        import logging
        logging.getLogger("arthur").info("voice model unloaded after %d idle minutes", IDLE_UNLOAD_S // 60)


def _engine():
    global _kokoro, _last_used
    import time
    _last_used = time.time()
    if _kokoro is None:
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
    return _kokoro


def _cache_path(text: str) -> Path:
    cfg = config.load()
    voice, fx, speed = cfg.get("kokoro_voice", "bm_george"), cfg.get("voice_fx", "arthur"), cfg.get("voice_speed", 1.0)
    intensity = cfg.get("voice_fx_intensity", 1.0)
    return CACHE / (hashlib.sha1(f"{voice}|{fx}|{intensity}|{speed}|{text}".encode()).hexdigest()[:16] + ".wav")


def render(text: str, voice: str | None = None, fx: str | None = None, speed: float | None = None,
           intensity: float | None = None) -> Path:
    cfg = config.load()
    voice = voice or cfg.get("kokoro_voice", "bm_george")
    fx = fx or cfg.get("voice_fx", "arthur")
    speed = speed or cfg.get("voice_speed", 1.0)
    intensity = intensity if intensity is not None else cfg.get("voice_fx_intensity", 1.0)
    chain = helmet(intensity) if fx == "helmet" else FX.get(fx, FX["arthur"])
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{voice}|{fx}|{intensity}|{speed}|{text}".encode()).hexdigest()[:16]
    out = CACHE / f"{key}.wav"
    if out.exists():
        return out
    import soundfile as sf

    samples, sr = _engine().create(text, voice=voice, speed=speed, lang="en-gb")
    raw = CACHE / f"{key}.raw.wav"
    sf.write(raw, samples, sr)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(raw), "-af", chain, str(out)],
                   check=True)
    raw.unlink(missing_ok=True)
    for old in sorted(CACHE.glob("*.wav"), key=lambda p: p.stat().st_mtime)[:-20]:
        old.unlink(missing_ok=True)  # keep the cache small
    return out


def duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def render_out_of_process(text: str) -> Path:
    import sys
    p = subprocess.run([sys.executable, "-m", "arthur", "tts"], input=text, capture_output=True, text=True,
                       cwd=str(config.ROOT), timeout=180)
    out = Path(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else None
    if p.returncode or not out or not out.exists():
        raise RuntimeError(p.stderr[-300:])
    return out


def sentences(text: str) -> list[str]:
    import re
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out, buf = [], ""
    for p in parts:  # glue very short fragments to the next so prosody stays natural
        buf = f"{buf} {p}".strip()
        if len(buf) > 25:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out or [text]


def prerender(text: str) -> None:
    for s in sentences(text):
        render(s)


class SpeechJob:
    """Streams speech sentence by sentence: the first one plays ~1 s after the call, the rest render while it talks.
    With stream=True more text can be added (add) while it's speaking, then finish() closes it.
    Popen-like (poll/terminate/wait) so the server can treat it like a process."""

    def __init__(self, text: str, stream: bool = False, after=None):
        import queue
        import threading
        self.inq: queue.Queue = queue.Queue()
        self.ready: queue.Queue = queue.Queue()
        self.stopped = False
        self.done = False
        self.proc = None
        self.duration = len(text.split()) / 2.6  # estimate for the HUD
        self.after = after  # play only once the previous speech has finished (no cutting him off)
        self.add(text)
        if not stream:
            self.finish()
        threading.Thread(target=self._render, daemon=True).start()
        threading.Thread(target=self._play, daemon=True).start()

    def add(self, text: str):
        for s in sentences(text) if text.strip() else []:
            self.inq.put(s)
        self.duration += len(text.split()) / 2.6 if self.inq.qsize() > 1 else 0

    def finish(self):
        self.inq.put("END")

    def _render(self):
        while not self.stopped:
            s = self.inq.get()
            if s == "END":
                break
            try:
                self.ready.put(render_via_worker(s))
            except Exception:  # noqa: BLE001 — voice helper broke: say it in the system voice rather than skip it
                self.ready.put(("say", s))
        self.ready.put("END")

    def _play(self):
        import time
        while self.after is not None and not self.after.done and not self.stopped:
            time.sleep(0.05)
        while not self.stopped:
            wav = self.ready.get()
            if wav == "END":
                break
            if wav is None:
                continue
            if isinstance(wav, tuple):
                self.proc = subprocess.Popen(["/usr/bin/say", "-v", config.load().get("voice", "Daniel"), "-r", "182", wav[1]],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.proc = subprocess.Popen(["/usr/bin/afplay", str(wav)])
            self.proc.wait()
        self.done = True

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        self.stopped = True
        self.inq.put("END")
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.done = True

    def wait(self):
        import time
        while not self.done:
            time.sleep(0.1)
        return 0


def speak(text: str, stream: bool = False, after=None):
    """Start speaking (streamed); queued after `after` if given. Returns (job, estimated seconds)."""
    try:
        job = SpeechJob(text, stream=stream, after=after)
        return job, job.duration
    except Exception:  # noqa: BLE001 — never go silent at 08:30
        cfg = config.load()
        p = subprocess.Popen(["/usr/bin/say", "-v", cfg["voice"], "-r", "182", text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return p, len(text.split()) / 3.0


def _speak_whole(text: str) -> tuple[subprocess.Popen, float]:
    """Start playback; returns (process, seconds)."""
    try:
        wav = render(text)  # in-process: model stays warm between replies, freed after 10 idle minutes
        return subprocess.Popen(["/usr/bin/afplay", str(wav)]), duration(wav)
    except Exception:  # noqa: BLE001 — never go silent at 08:30
        cfg = config.load()
        p = subprocess.Popen(["/usr/bin/say", "-v", cfg["voice"], "-r", "182", text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return p, len(text.split()) / 3.0

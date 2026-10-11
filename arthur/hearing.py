"""Better hearing, all local and free:

- Silero VAD (2 MB, bundled with faster-whisper): knows speech from noise -> accurate end-of-speech + trims silence.
- DeepFilterNet (bin/deep-filter, official release binary): removes non-speech noise (fans, traffic, hum) before
  transcription. It can't remove other people talking — that needs speaker verification.
- Whisper large-v3-turbo on the Apple GPU (MLX): far more accurate with accents and names. Loaded in the background
  while you're still speaking, freed after 10 idle minutes.
Falls back to the small CPU model if anything here fails.
"""
import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger("arthur.hearing")
RATE = 16000
TURBO = str(config.ROOT / "models/whisper-turbo")  # local copy of mlx-community/whisper-large-v3-turbo
DEEP_FILTER = config.ROOT / "bin/deep-filter"
IDLE_UNLOAD_S = 600

_vad = None
_turbo_ready = threading.Event()
_turbo_failed = threading.Event()
_turbo_used = 0.0


# ---------- voice activity (Silero) ----------
def vad():
    global _vad
    if _vad is None:
        from faster_whisper.vad import get_vad_model
        _vad = get_vad_model()
    return _vad


def speech_spans(audio: np.ndarray, min_silence_ms: int = 500) -> list[dict]:
    """[{start, end}] in samples where someone is speaking."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    if audio.size < RATE // 4:
        return []
    f = audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio
    return get_speech_timestamps(f, VadOptions(threshold=0.5, min_silence_duration_ms=min_silence_ms,
                                               speech_pad_ms=150), sampling_rate=RATE)


def finished_speaking(audio: np.ndarray, tail_s: float = 0.8) -> tuple[bool, bool]:
    """(started, finished): has speech begun, and has it been followed by `tail_s` of non-speech."""
    spans = speech_spans(audio)
    if not spans:
        return False, False
    return True, (audio.size - spans[-1]["end"]) / RATE >= tail_s


def trim(audio: np.ndarray) -> np.ndarray:
    spans = speech_spans(audio, 300)
    if not spans:
        return audio
    return audio[max(0, spans[0]["start"] - 1600): spans[-1]["end"] + 1600]


# ---------- noise removal (DeepFilterNet) ----------
def denoise(audio: np.ndarray, force: bool = False) -> np.ndarray:
    """int16 16 kHz in -> int16 16 kHz out. Off by default (config `denoise`): in tests it hurt whisper accuracy."""
    if not DEEP_FILTER.exists() or not (force or config.load().get("denoise", False)) or audio.size < RATE // 2:
        return audio
    import soundfile as sf
    try:
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "in.wav", Path(d) / "out"
            hi = np.interp(np.arange(0, audio.size, 1 / 3), np.arange(audio.size), audio.astype(np.float32))
            sf.write(src, hi.astype(np.int16), 48000)
            subprocess.run([str(DEEP_FILTER), "-o", str(out), str(src)], capture_output=True, timeout=30, check=True)
            clean, _ = sf.read(out / "in.wav", dtype="float32")
            return (clean[::3] * 32767).clip(-32768, 32767).astype(np.int16)
    except Exception as e:  # noqa: BLE001
        log.warning("denoise skipped: %s", e)
        return audio


# ---------- transcription (MLX turbo) ----------
# MLX binds GPU streams to the thread that created them, so every turbo call (load, transcribe, unload)
# runs on this one worker thread.
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

_mlx = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")


def _load():
    global _turbo_used
    if _turbo_ready.is_set():
        return
    try:
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder
        ModelHolder.get_model(TURBO, mx.float16)
        _turbo_ready.set()
        _turbo_used = time.time()
    except Exception as e:  # noqa: BLE001
        log.warning("turbo load failed: %s", e)
        _turbo_failed.set()


def preload_turbo():
    """Start loading the turbo model in the background (call at wake time)."""
    if config.load().get("stt_model", "turbo") != "turbo" or not Path(TURBO, "weights.safetensors").exists():
        _turbo_failed.set()
        return
    if not _turbo_ready.is_set():
        _turbo_failed.clear()
        _mlx.submit(_load)


def _unload():
    from mlx_whisper.transcribe import ModelHolder
    import mlx.core as mx
    ModelHolder.model, ModelHolder.model_path = None, None
    _turbo_ready.clear()
    mx.clear_cache()
    log.info("turbo model unloaded (idle)")


def unload_if_idle():
    if _turbo_ready.is_set() and time.time() - _turbo_used > IDLE_UNLOAD_S:
        _mlx.submit(_unload).result()


def _run(audio: np.ndarray, prompt: str) -> str:
    import mlx_whisper
    r = mlx_whisper.transcribe(audio.astype(np.float32) / 32768.0, path_or_hf_repo=TURBO, language="en",
                               initial_prompt=prompt, condition_on_previous_text=False)
    return (r.get("text") or "").strip()


def transcribe_turbo(audio: np.ndarray, prompt: str) -> str | None:
    """Returns text, or None if turbo isn't available (caller falls back to the small model)."""
    global _turbo_used
    preload_turbo()
    if _turbo_failed.is_set():
        return None
    try:
        text = _mlx.submit(_run, audio, prompt).result(timeout=30)  # queued behind the load if still loading
        _turbo_used = time.time()
        return text if _turbo_ready.is_set() else None
    except Exception as e:  # noqa: BLE001
        log.warning("turbo transcribe failed: %s", e)
        return None

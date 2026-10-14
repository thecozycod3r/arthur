"""Only respond to Tony: a voice fingerprint (ERes2Net speaker embedding, sherpa-onnx, offline, ~30 MB).
Enrolled from the training recordings (data/wake_samples). Each wake / follow-up is compared to it; other
voices (TV, colleagues) are ignored. `voiceprint_threshold` in config.json; set `voiceprint: false` to disable."""
import glob

import numpy as np
import soundfile as sf

from . import config

MODEL = config.ROOT / "models/speaker.onnx"
PRINT = config.DATA / "voiceprint.npy"
_ext = None


def _extractor():
    global _ext
    if _ext is None:
        import sherpa_onnx
        _ext = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL), num_threads=1))
    return _ext


def embed(audio: np.ndarray) -> np.ndarray | None:
    if audio.size < 16000 * 0.5:
        return None
    ex = _extractor()
    s = ex.create_stream()
    s.accept_waveform(16000, (audio.astype(np.float32) / 32768.0) if audio.dtype == np.int16 else audio)
    s.input_finished()
    if not ex.is_ready(s):
        return None
    v = np.array(ex.compute(s), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def enroll(paths: list[str] | None = None) -> int:
    paths = paths or sorted(glob.glob(str(config.DATA / "wake_samples/pos_*.wav")) +
                            glob.glob(str(config.DATA / "wake_samples/read_*.wav")))
    vs = [v for v in (embed(sf.read(p, dtype="int16")[0]) for p in paths) if v is not None]
    if not vs:
        raise RuntimeError("no usable recordings to enroll")
    m = np.mean(vs, axis=0)
    np.save(PRINT, m / np.linalg.norm(m))
    return len(vs)


def speech_only(audio: np.ndarray) -> np.ndarray:
    """Just the spoken parts. Silence, the chime and room noise around a short "yes" drown the voice otherwise
    (12 Oct: the owner's real "yes" scored 0.04 / 0.25 on the whole recording and sends were refused)."""
    from . import hearing
    try:
        spans = hearing.speech_spans(audio, 200)
    except Exception:  # noqa: BLE001
        return audio
    if not spans:
        return audio
    return np.concatenate([audio[max(0, s["start"] - 800): s["end"] + 800] for s in spans])


def score(audio: np.ndarray) -> float | None:
    """Cosine similarity to your voiceprint (≈1 = you). None if it can't judge (too short / not enrolled)."""
    if not PRINT.exists():
        return None
    v = embed(speech_only(audio))
    return None if v is None else float(np.dot(v, np.load(PRINT)))


def is_me(audio: np.ndarray) -> bool:
    cfg = config.load()
    if not cfg.get("voiceprint", True):
        return True
    s = score(audio)
    return True if s is None else s >= cfg.get("voiceprint_threshold", 0.30)

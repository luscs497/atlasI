"""
Voz do ATLAS via Kokoro (kokoro-onnx) — TTS local, gratuito e leve.

Kokoro é um modelo de TTS pequeno (82M) que roda no `onnxruntime` que já usamos
para o Whisper — sem PyTorch. Prosódia bem mais natural que o Piper. As vozes em
PT-BR (pf_dora / pm_alex / pm_santa) são recentes/experimentais, então a escolha
final é do ouvido do usuário.

Mesma interface dos outros motores: `synthesize(text) -> bytes` (WAV completo),
`from_config`, `label`. Aceita um `fallback` (Piper) para nunca deixar o ATLAS
mudo se algo falhar. Modelos em ~/.atlas/models/kokoro/.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np

MODELS_DIR = Path.home() / ".atlas" / "models" / "kokoro"
_MODEL = "kokoro-v1.0.onnx"
_VOICES = "voices-v1.0.bin"


def _wav_from_float(samples, sample_rate: int) -> bytes:
    """Converte áudio float32 [-1,1] do Kokoro em WAV PCM S16LE (bytes)."""
    arr = np.asarray(samples, dtype=np.float32)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm)
    return buf.getvalue()


class KokoroSpeaker:
    """Sintetizador de fala via Kokoro (onnx), com fallback para o Piper."""

    def __init__(self, model_path: Path, voices_path: Path, voice: str,
                 lang: str = "pt-br", speed: float = 1.0, fallback=None) -> None:
        from kokoro_onnx import Kokoro

        self.voice = voice
        self.lang = lang
        self.speed = speed
        self._fallback = fallback
        self._k = Kokoro(str(model_path), str(voices_path))

    @property
    def label(self) -> str:
        return f"kokoro {self.voice} ({self.lang})"

    def synthesize(self, text: str) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        try:
            samples, sr = self._k.create(
                text, voice=self.voice, speed=self.speed, lang=self.lang
            )
            return _wav_from_float(samples, sr)
        except Exception as exc:  # nunca fica mudo — cai no fallback (Piper)
            print(f"[kokoro] falha ({exc!r}); usando fallback.", flush=True)
            return self._fallback.synthesize(text) if self._fallback else b""

    @classmethod
    def from_config(cls, config: dict, fallback=None) -> "KokoroSpeaker | None":
        """Constrói se os modelos estiverem baixados; senão None (usa fallback)."""
        model_path = MODELS_DIR / _MODEL
        voices_path = MODELS_DIR / _VOICES
        if not model_path.exists() or not voices_path.exists():
            return None
        return cls(
            model_path=model_path,
            voices_path=voices_path,
            voice=config.get("kokoro_voice", "pm_alex"),
            lang=config.get("kokoro_lang", "pt-br"),
            speed=float(config.get("kokoro_speed", 1.0)),
            fallback=fallback,
        )

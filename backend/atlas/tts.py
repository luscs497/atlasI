"""
Voz do ATLAS (TTS) — síntese de fala local via Piper.

Local/gratuito e offline, espelhando a escolha provisória do cérebro (Gemini).
O `tts_engine=elevenlabs` da config é o alvo futuro; para trocar, basta um
"Speaker" equivalente com o mesmo método `synthesize()` devolvendo WAV em bytes.

A síntese é rápida, mas ainda assim os chamadores devem executá-la em
`asyncio.to_thread` para não bloquear o event loop.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

MODELS_DIR = Path.home() / ".atlas" / "models" / "piper"


class Speaker:
    """Sintetizador de fala baseado no Piper (voz local)."""

    def __init__(self, model_path: Path) -> None:
        # Import tardio: só custa quando a voz é realmente usada.
        from piper import PiperVoice

        self.model_path = model_path
        self._voice = PiperVoice.load(str(model_path))

    @property
    def label(self) -> str:
        return f"piper {self.model_path.stem}"

    def synthesize(self, text: str) -> bytes:
        """Converte texto em um WAV completo (bytes), pronto para tocar na UI."""
        text = (text or "").strip()
        if not text:
            return b""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            self._voice.synthesize_wav(text, wf)
        return buf.getvalue()

    @classmethod
    def from_config(cls, config: dict) -> "Speaker | None":
        """
        Carrega a voz definida em `config['tts_voice']`. Retorna None se o modelo
        não estiver baixado (voz simplesmente fica indisponível, sem quebrar).
        """
        voice = config.get("tts_voice", "pt_BR-faber-medium")
        model_path = MODELS_DIR / f"{voice}.onnx"
        if not model_path.exists():
            return None
        return cls(model_path)

"""
Voz premium do ATLAS (TTS) — síntese de fala via API do ElevenLabs.

Espelho do `atlas.tts.Speaker` (Piper): mesma interface `synthesize()` que
devolve um WAV completo em bytes, pronto para a UI tocar. É o alvo "de filme":
uma voz grave/madura (por padrão a britânica "George").

Pedimos PCM cru (`pcm_24000`, S16LE mono) e embrulhamos num container WAV com o
módulo `wave` da stdlib — assim mantemos o contrato `audio/wav` da UI sem
dependência nova nem conversão de MP3.

A cota gratuita é limitada (~10 mil caracteres/mês). Quando ela acaba (ou a rede
falha), caímos automaticamente no `fallback` (Piper local), para o ATLAS nunca
ficar mudo. A chamada é de rede/bloqueante — os chamadores já a rodam em
`asyncio.to_thread`.
"""

from __future__ import annotations

import io
import json
import os
import urllib.request
import wave
from urllib.error import HTTPError, URLError

API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
# Formato pedido à API: PCM cru, 24 kHz, 16-bit, mono.
OUTPUT_FORMAT = "pcm_24000"
SAMPLE_RATE = 24000

# Voz padrão: "George" — britânico, grave e cativante (escolha do usuário).
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
DEFAULT_VOICE_NAME = "George"
DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_SETTINGS = {
    "stability": 0.85,  # voz firme/constante (menos variação entre falas)
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
}


class ElevenSpeaker:
    """Sintetizador de fala via ElevenLabs, com fallback opcional para o Piper."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str,
        voice_name: str = "",
        settings: dict | None = None,
        fallback: object | None = None,
    ) -> None:
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.voice_name = voice_name or voice_id
        self.settings = settings or dict(DEFAULT_SETTINGS)
        # `fallback` é um Speaker (Piper) com o mesmo `synthesize()`, ou None.
        self.fallback = fallback

    @property
    def label(self) -> str:
        return f"elevenlabs {self.voice_name}"

    def synthesize(self, text: str) -> bytes:
        """Converte texto em WAV (bytes). Cai no fallback se a API falhar."""
        text = (text or "").strip()
        if not text:
            return b""
        try:
            pcm = self._request_pcm(text)
            return self._wrap_wav(pcm)
        except (HTTPError, URLError, OSError) as exc:
            detalhe = ""
            if isinstance(exc, HTTPError):
                # 401 chave inválida, 402 sem crédito, 429 limite de taxa.
                detalhe = f" (HTTP {exc.code})"
            print(f"[voz] ElevenLabs indisponível{detalhe}: {exc!r}", flush=True)
            if self.fallback is not None:
                print(f"[voz] usando fallback: {self.fallback.label}", flush=True)
                return self.fallback.synthesize(text)
            return b""

    def _request_pcm(self, text: str) -> bytes:
        """Chama a API e devolve o áudio PCM cru (S16LE, 24 kHz, mono)."""
        url = f"{API_BASE}/{self.voice_id}?output_format={OUTPUT_FORMAT}"
        body = json.dumps(
            {
                "text": text,
                "model_id": self.model,
                "voice_settings": self.settings,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/pcm",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    @staticmethod
    def _wrap_wav(pcm: bytes) -> bytes:
        """Embrulha PCM S16LE mono num container WAV (o que a UI espera)."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm)
        return buf.getvalue()

    @classmethod
    def from_config(
        cls, config: dict, fallback: object | None = None
    ) -> "ElevenSpeaker | None":
        """
        Constrói a partir da config + variável de ambiente ELEVENLABS_API_KEY.
        Retorna None se a chave não estiver definida (o chamador cai no Piper).
        """
        api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            voice_id=config.get("elevenlabs_voice_id", DEFAULT_VOICE_ID),
            model=config.get("elevenlabs_model", DEFAULT_MODEL),
            voice_name=config.get("elevenlabs_voice_name", DEFAULT_VOICE_NAME),
            settings=config.get("elevenlabs_voice_settings") or dict(DEFAULT_SETTINGS),
            fallback=fallback,
        )

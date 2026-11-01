"""
Ouvido do ATLAS (STT) — transcrição de fala local via faster-whisper.

Roda 100% offline na CPU (compute_type int8). Aceita o áudio gravado pela UI
em bytes (qualquer contêiner que o PyAV decodifique — ex.: webm/opus do
MediaRecorder, ou WAV) e devolve o texto reconhecido.

O modelo demora alguns segundos para carregar; por isso é carregado uma vez e
reutilizado. As chamadas de transcrição devem rodar em `asyncio.to_thread`.

Qualidade: o modelo padrão é `small` — bem mais preciso em PT-BR que o `base`,
ainda viável em CPU (int8). Para máxima precisão troque para `medium` na config
(`~/.atlas/config.json`, chave `stt_model`), ao custo de mais latência. Um
`initial_prompt` em português enviesa a grafia e o contexto (inclusive o nome
"ATLAS").
"""

from __future__ import annotations

import io
import os
from pathlib import Path

MODELS_DIR = Path.home() / ".atlas" / "models" / "whisper"

# Enviesa o reconhecimento para PT-BR e o domínio do assistente.
DEFAULT_PROMPT = (
    "Transcrição de uma conversa em português do Brasil com um assistente "
    "pessoal chamado ATLAS."
)


class Transcriber:
    """Reconhecedor de fala baseado no faster-whisper."""

    def __init__(
        self,
        model_size: str = "small",
        language: str = "pt",
        initial_prompt: str = DEFAULT_PROMPT,
    ) -> None:
        from faster_whisper import WhisperModel

        self.model_size = model_size
        self.language = language
        self.initial_prompt = initial_prompt
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        self._model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            download_root=str(MODELS_DIR),
            # Usa vários núcleos para reduzir a latência do modelo maior.
            cpu_threads=min(8, os.cpu_count() or 4),
        )

    def transcribe(self, audio: bytes) -> str:
        """Transcreve áudio (bytes de um arquivo/contêiner) para texto."""
        if not audio:
            return ""
        segments, _ = self._model.transcribe(
            io.BytesIO(audio),
            language=self.language,
            beam_size=5,
            # Enviesa grafia/pontuação para PT-BR e o domínio do ATLAS.
            initial_prompt=self.initial_prompt,
            # Não arrasta contexto entre segmentos: evita "alucinar" repetições.
            condition_on_previous_text=False,
            # Descarta silêncio nas pontas, mas sem cortar fala real.
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            # Fallback de temperatura ajuda quando a 1ª decodificação falha.
            temperature=[0.0, 0.2, 0.4],
        )
        return "".join(seg.text for seg in segments).strip()

    @classmethod
    def from_config(cls, config: dict) -> "Transcriber":
        return cls(
            model_size=config.get("stt_model", "small"),
            language=config.get("stt_language", "pt"),
            initial_prompt=config.get("stt_initial_prompt", DEFAULT_PROMPT),
        )

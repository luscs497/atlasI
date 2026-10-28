"""
STT do capacete — faster-whisper sobre PCM cru (sem contêiner, sem decodificar).

Mesmos modelos do JARVIS desktop (`~/.jarvis/models/whisper`), mas configurado
para LATÊNCIA: beam guloso, sem VAD interno (o `Segmenter` já cortou a fala) e
sem timestamps. Inclui um filtro contra as "alucinações" clássicas do Whisper em
ruído ("Obrigado.", "Legendas pela comunidade Amara.org"…).
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from pathlib import Path

from helmet.audio.resample import pcm_to_float, resample_pcm

MODELS_DIR = Path.home() / ".jarvis" / "models" / "whisper"

PROMPT = (
    "Conversa em português do Brasil com o assistente pessoal JARVIS. "
    "Jarvis, que horas são?"
)

# O Whisper às vezes "transcreve" o próprio prompt sobre silêncio.
_PROMPTS: set[str] = set()  # preenchido no 1º uso (normalize vem depois)

# Frases que o Whisper inventa sobre ruído/silêncio. Comparadas normalizadas.
_HALLUCINATIONS = {
    # inglês
    "thank you", "thanks for watching", "thank you for watching", "you",
    "please subscribe", "subscribe to the channel", "music", "applause", "laughter",
    # português
    "obrigado", "obrigada", "obrigado por assistir", "obrigada por assistir",
    "legendas pela comunidade amara org", "legenda adriana zanotto",
    "inscreva se no canal", "inscrevase", "tchau", "ate a proxima",
    "e ai", "musica", "risos", "aplausos", "",
}


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", t).split())


def _fill_prompts() -> None:
    from helmet.i18n import TEXTS

    _PROMPTS.update(normalize(v["stt_prompt"]) for v in TEXTS.values())
    _PROMPTS.add(normalize(PROMPT))


def is_hallucination(text: str) -> bool:
    if not _PROMPTS:
        _fill_prompts()
    n = normalize(text)
    return n in _HALLUCINATIONS or n in _PROMPTS


class SpeechRecognizer:
    def __init__(
        self,
        model_size: str = "small",
        language: str = "pt",
        beam_size: int = 1,
        cpu_threads: int = 8,
        initial_prompt: str = PROMPT,
    ) -> None:
        from faster_whisper import WhisperModel

        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        self.model_size = model_size
        self.language = language
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt
        self._model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            download_root=str(MODELS_DIR),
            cpu_threads=min(cpu_threads, os.cpu_count() or 4),
        )
        self._lock = threading.Lock()  # um enunciado por vez no modelo

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = 16_000) -> str:
        if not pcm:
            return ""
        if sample_rate != 16_000:
            pcm = resample_pcm(pcm, sample_rate, 16_000)
        audio = pcm_to_float(pcm)
        with self._lock:
            segments, _ = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=self.beam_size,
                initial_prompt=self.initial_prompt,
                condition_on_previous_text=False,
                without_timestamps=True,
                vad_filter=False,
                temperature=0.0,
            )
            parts = [
                s.text for s in segments
                # Segmento que o próprio modelo acha que é "não-fala".
                if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)
            ]
        text = "".join(parts).strip()
        return "" if is_hallucination(text) else text

    def warmup(self) -> None:
        """Primeira inferência é lenta (alocação); faz uma em silêncio no boot."""
        self.transcribe_pcm(b"\x00\x00" * 16_000)

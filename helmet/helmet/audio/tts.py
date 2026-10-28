"""
TTS do capacete — a MESMA voz do JARVIS desktop (Kokoro, com Piper de reserva),
entregue como PCM s16le mono na taxa que o destino pedir.

A resposta é falada FRASE A FRASE: a 1ª frase começa a tocar enquanto as
seguintes ainda estão sendo sintetizadas (corta a latência percebida).
"""

from __future__ import annotations

import re
import threading

from helmet import jarvis_path  # noqa: F401  (deixa `jarvis` importável)
from helmet.audio.resample import resample_pcm, wav_to_pcm

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️]"
)
_SENT = re.compile(r"(?<=[.!?…;:])\s+")
_GLUE = {
    "a", "o", "as", "os", "um", "uma", "de", "do", "da", "dos", "das", "em", "no",
    "na", "nos", "nas", "e", "ou", "que", "com", "para", "pra", "por", "pelo",
    "pela", "ao", "à", "se", "sobre", "mas", "seu", "sua", "meu", "minha",
    # inglês
    "the", "an", "of", "to", "in", "and", "or", "for", "with", "on", "at", "my",
    "your", "his", "her", "its", "our", "their", "from", "by", "as", "is", "are",
}


def clean_for_speech(text: str) -> str:
    """Tira markdown/emojis/URLs, que soam mal (ou são lidos literalmente)."""
    t = _EMOJI.sub("", text or "")
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"[`*_#>|]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.M)
    return " ".join(t.split())


def _split_long(sentence: str, max_chars: int) -> list[str]:
    """Frase longa demais -> pedaços nas vírgulas (sem cortar palavras)."""
    if len(sentence) <= max_chars:
        return [sentence]
    out: list[str] = []
    buf = ""
    for piece in re.split(r"(?<=,)\s+", sentence):
        if buf and len(buf) + 1 + len(piece) > max_chars:
            out.append(buf)
            buf = piece
        else:
            buf = f"{buf} {piece}".strip()
    if buf:
        out.append(buf)
    return out


def _split_words(chunk: str, target: int) -> list[str]:
    """Último recurso p/ o 1º pedaço: corta num espaço perto de `target`."""
    if len(chunk) <= target * 1.4:
        return [chunk]
    cut = chunk.rfind(" ", 0, target + 1)
    if cut < target // 2:
        cut = chunk.find(" ", target)
    if cut <= 0:
        return [chunk]
    # Não termina o pedaço em palavra de ligação ("…o Homem de | Ferro").
    while True:
        prev = chunk.rfind(" ", 0, cut)
        if prev <= 0 or chunk[prev + 1:cut].lower() not in _GLUE:
            break
        cut = prev
    return [chunk[:cut], chunk[cut + 1:]]


def split_sentences(
    text: str, min_chars: int = 25, first_max: int = 50, max_chars: int = 160
) -> list[str]:
    """
    Quebra em frases; junta as muito curtas na seguinte (prosódia melhor) e
    parte as longas nas vírgulas. O 1º pedaço é mantido CURTO (`first_max`):
    é ele que define quanto tempo o usuário espera até ouvir algo.
    """
    raw = [p.strip() for p in _SENT.split(clean_for_speech(text)) if p.strip()]
    parts: list[str] = []
    for i, p in enumerate(raw):
        parts += _split_long(p, first_max if i == 0 else max_chars)
    # Kokoro: ~0,2 s + ~12 ms/caractere. 1º pedaço ~50 chars => voz em ~0,6 s.
    if parts:
        parts[:1] = _split_words(parts[0], first_max)
    out: list[str] = []
    buf = ""
    for p in parts:
        buf = f"{buf} {p}".strip()
        if len(buf) >= min_chars:
            out.append(buf)
            buf = ""
    if buf:
        if out and len(buf) < min_chars:
            out[-1] = f"{out[-1]} {buf}"
        else:
            out.append(buf)
    return out


class VoiceSynth:
    def __init__(self, speaker) -> None:  # noqa: ANN001  (KokoroSpeaker/Speaker)
        self._speaker = speaker
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return getattr(self._speaker, "label", "?")

    def synth_pcm(self, text: str, sample_rate: int) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        with self._lock:
            wav = self._speaker.synthesize(text)
        if not wav:
            return b""
        pcm, sr = wav_to_pcm(wav)
        return resample_pcm(pcm, sr, sample_rate)

    @classmethod
    def from_jarvis_config(cls, voice: str = "", lang: str = "") -> "VoiceSynth | None":
        """
        Constrói com a voz configurada no `~/.jarvis/config.json` do desktop, ou
        com `voice`/`lang` do capacete (sobrepõe só aqui; o arquivo não muda).
        """
        from jarvis.config import load_config
        from jarvis.kokoro_tts import KokoroSpeaker
        from jarvis.tts import Speaker

        config = dict(load_config())
        if voice:
            config["kokoro_voice"] = voice
            config["kokoro_lang"] = lang or config.get("kokoro_lang", "pt-br")
        piper = None
        try:
            piper = Speaker.from_config(config)
        except Exception:
            piper = None
        kokoro = None
        if config.get("tts_engine", "kokoro") in ("kokoro", "elevenlabs", "xtts"):
            # XTTS é lento demais p/ capacete e ElevenLabs está sem crédito:
            # o capacete usa sempre o Kokoro (rápido/local) quando disponível.
            kokoro = KokoroSpeaker.from_config(config, fallback=piper)
        speaker = kokoro or piper
        return cls(speaker) if speaker is not None else None

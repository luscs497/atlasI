"""
Detecção da palavra de ativação ("Jarvis") e de comandos de dormir no TEXTO
transcrito.

Por que no texto (e não um modelo de wake word dedicado)? O Whisper já roda em
cada enunciado e entende "Jarvis" bem com o prompt certo; e assim "Jarvis, que
horas são?" vira ativação + comando numa ÚNICA passada (sem esperar um bipe).
Aceita as grafias que o Whisper costuma produzir em PT-BR (jarvis, jarves,
djarvis, jarbis…) com casamento aproximado.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from helmet.audio.stt import normalize

WAKE = "jarvis"
_VARIANTS = {
    "jarvis", "jarvi", "jarves", "jarviz", "jarvys", "djarvis", "jarbis",
    "jarvas", "jervis", "gervis", "jarviss", "jarvice", "jarviis", "javis",
    "charvis", "jarvisz",
}
# Palavras parecidas que NÃO são o nome (evita falso positivo do difflib).
_BLOCK = {"jarras", "jardins", "jardim", "jarra", "jarros", "garvis"}

_TOKEN = re.compile(r"\w+", re.UNICODE)


@dataclass
class WakeMatch:
    word: str  # como apareceu no texto
    before: str  # o que veio antes (ex.: "ok", "ei")
    command: str  # o que veio depois — o pedido em si (pode ser vazio)


def _is_wake_token(tok: str) -> bool:
    n = normalize(tok)
    if n in _VARIANTS:
        return True
    if n in _BLOCK or not 4 <= len(n) <= 8:
        return False
    return difflib.SequenceMatcher(None, n, WAKE).ratio() >= 0.8


def find_wake(text: str) -> WakeMatch | None:
    for m in _TOKEN.finditer(text or ""):
        if _is_wake_token(m.group()):
            command = text[m.end():].lstrip(" ,.!?;:-—…").strip()
            return WakeMatch(m.group(), text[: m.start()].strip(), command)
    return None


def strip_leading_wake(text: str) -> str:
    """Já ativo, "Jarvis, abre X" vira só "abre X" (o nome não é o pedido)."""
    m = find_wake(text)
    if m and len(normalize(m.before).split()) <= 2:  # "ok jarvis", "ei jarvis"
        return m.command
    return text.strip()


_SLEEP = re.compile(
    r"^(?:(?:ok|ta|tá|certo|beleza|valeu|obrigad[oa])[\s,]*)*"
    r"(?:(?:pode|podi|podji|pod|vai)\s+)?"
    r"(?:deslig(?:ar|a)|dormir|descansar|desativar|parar de (?:ouvir|escutar)|"
    r"encerrar(?: a)? (?:escuta|conversa)|(?:e|é) s[oó] isso|s[oó] isso)"
    r"(?:\s+(?:por (?:agora|hoje|enquanto)|jarvis|obrigad[oa]))*[\s.!]*$",
    re.IGNORECASE,
)


_SLEEP_EN = re.compile(
    r"^(?:(?:ok|okay|alright|right|thanks|thank you|cheers)[\s,]*)*"
    r"(?:(?:you can|you may|please)\s+)?"
    r"(?:(?:go|go back|return|get back) to sleep|sleep|stand down|shut down|power down|that s all|thats all|"
    r"that will be all|that ll be all|that s it|goodbye|good bye|bye|stop listening)"
    r"(?:\s+(?:for now|for today|jarvis|thanks|thank you|then))*[\s.!]*$",
    re.IGNORECASE,
)


def is_sleep_command(text: str) -> bool:
    n = normalize(text)
    return bool(_SLEEP.match(n) or _SLEEP_EN.match(n))

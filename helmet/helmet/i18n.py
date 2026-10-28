"""
Idioma do capacete (`HELMET_LANGUAGE`): textos fixos, prompt do Whisper e voz
padrão. O JARVIS do desktop não muda: o idioma vale só para o capacete.

O prompt-base do cérebro (do desktop) é em português; no capacete em inglês, o
adendo de canal manda o GPT responder SEMPRE em inglês, no estilo do JARVIS.
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["pt", "en"]

TEXTS: dict[str, dict[str, str]] = {
    "pt": {
        "stt_prompt": "Conversa em português do Brasil com o assistente pessoal JARVIS. "
                      "Jarvis, que horas são?",
        "error_reply": "Tive um problema para processar isso agora, senhor. "
                       "Tente de novo em instantes.",
        "empty_reply": "Desculpe, senhor, não consegui formular uma resposta.",
        "display_error": "Não consegui.",
        "listening": "Ouvindo...",
        "standby": "Em espera.",
        "warmup": "Pronto.",
    },
    "en": {
        "stt_prompt": "A conversation in English with the personal assistant JARVIS. "
                      "Jarvis, what time is it?",
        "error_reply": "I'm afraid I ran into a problem, sir. Please try again in a moment.",
        "empty_reply": "My apologies, sir, I couldn't come up with an answer.",
        "display_error": "Couldn't do it.",
        "listening": "Listening...",
        "standby": "Standing by.",
        "warmup": "Ready.",
    },
}

# Voz Kokoro padrão por idioma ("" = a mesma do desktop, do config.json).
DEFAULT_VOICE: dict[str, tuple[str, str]] = {
    "pt": ("", ""),
    "en": ("bm_daniel", "en-gb"),  # britânico, escolhido pelo Lucas
}


def t(lang: str, key: str) -> str:
    return TEXTS.get(lang, TEXTS["pt"])[key]

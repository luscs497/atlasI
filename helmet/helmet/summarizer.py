"""Título + descrição curtos de uma conversa encerrada (OpenAI)."""

from __future__ import annotations

import json
import os

_PROMPT = (
    "Você recebe a transcrição de uma conversa por voz entre um usuário e o JARVIS "
    "(assistente pessoal). Gere um TÍTULO curto (até 60 caracteres, sem aspas, sem "
    "ponto final) e uma DESCRIÇÃO curta (1 ou 2 frases, até 200 caracteres) que "
    "resumam os assuntos principais. Português do Brasil. Responda APENAS com JSON: "
    '{"titulo": "...", "descricao": "..."}'
)

# Transcrições enormes: manda começo + fim (o miolo raramente muda o título).
_MAX_CHARS = 14_000


def _clip(transcript: str) -> str:
    if len(transcript) <= _MAX_CHARS:
        return transcript
    half = _MAX_CHARS // 2
    return transcript[:half] + "\n[…trecho omitido…]\n" + transcript[-half:]


class OpenAISummarizer:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        from openai import AsyncOpenAI

        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, timeout=60)

    async def __call__(self, transcript: str) -> tuple[str, str]:
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": _clip(transcript)},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        title = str(data.get("titulo") or "").strip().strip('"').rstrip(".")[:120]
        desc = str(data.get("descricao") or "").strip()[:400]
        if not title:
            raise ValueError(f"resposta sem título: {data!r}")
        return title, desc

    @classmethod
    def from_env(cls, model: str) -> "OpenAISummarizer | None":
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        return cls(key, model) if key else None

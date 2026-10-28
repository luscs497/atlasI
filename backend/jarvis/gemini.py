"""
Cérebro do JARVIS via API do Google Gemini (gratuita).

Encapsula uma sessão de chat com histórico e a persona do JARVIS. A chamada é
assíncrona (SDK `google-genai`, cliente `aio`) para nunca travar o event loop do
servidor WebSocket. Um lock serializa as mensagens — o assistente é de usuário
único e as respostas devem sair em ordem.

Memória de longo prazo (`jarvis.memory.Memory`): os fatos duráveis sobre o
usuário são injetados na persona; o histórico recente é recarregado no chat ao
subir o backend (continuidade entre sessões). Após cada troca, uma chamada extra
e barata ao Gemini extrai novos fatos em segundo plano. O usuário também pode
comandar diretamente ("lembre-se que…", "esqueça…").

A troca por outro provedor (ex.: Claude) no futuro é só criar um "brain"
equivalente com o mesmo método `reply()`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from google import genai
from google.genai import types

from jarvis.memory import Memory

# Gatilhos de comando explícito (início da fala do usuário).
_REMEMBER_RE = re.compile(
    r"^\s*(?:lembre[- ]se|lembra|lembre|anota|anote|guarde|não esqueça)"
    r"(?:\s+(?:disso|que|de que|o seguinte))?[:,]?\s+(.*)",
    re.IGNORECASE | re.DOTALL,
)
_FORGET_RE = re.compile(
    r"^\s*(?:esqueça|esquece|esqueca|pode esquecer)"
    r"(?:\s+(?:que|sobre|do|da|o|a))?[:,]?\s+(.*)",
    re.IGNORECASE | re.DOTALL,
)


def _system_prompt(usuario_nome: str, facts_block: str) -> str:
    base = (
        f"Você é o JARVIS, o assistente pessoal de {usuario_nome}, inspirado no "
        "assistente do Homem de Ferro. Fale em português do Brasil. Quando for "
        "natural se dirigir ao usuário, trate-o apenas por 'senhor' — nunca "
        "'senhor {nome}'. Não force o 'senhor' em toda frase: use com parcimônia, "
        "só quando vier ao caso (cumprimentos, confirmações, respostas diretas). "
        "Seja conciso, direto e prestativo, com um toque sutil de elegância e "
        "humor britânico — sem exageros. Respostas curtas por padrão (1 a 3 frases), a menos que peçam "
        "detalhes. Nunca invente fatos; se não souber, diga com naturalidade. "
        "Você roda localmente no Pop!_OS (Linux) do usuário."
    )
    return base + facts_block


class GeminiBrain:
    """Sessão de conversa com o Gemini, mantendo histórico, persona e memória."""

    def __init__(
        self, api_key: str, model: str, usuario_nome: str, memory: Memory
    ) -> None:
        self.model = model
        self.usuario_nome = usuario_nome
        self.memory = memory
        self._client = genai.Client(api_key=api_key)
        self._config = types.GenerateContentConfig(
            system_instruction=_system_prompt(usuario_nome, memory.facts_block()),
            temperature=0.7,
            max_output_tokens=800,
        )
        self._chat = self._client.aio.chats.create(
            model=model, config=self._config, history=self._boot_history()
        )
        self._lock = asyncio.Lock()

    def _boot_history(self) -> list[types.Content]:
        """Converte o histórico salvo em `Content` do Gemini (user/model)."""
        contents: list[types.Content] = []
        for h in self.memory.boot_history():
            role = "user" if h.get("role") == "user" else "model"
            contents.append(
                types.Content(role=role, parts=[types.Part(text=h["texto"])])
            )
        # O Gemini exige que o histórico comece por um turno do usuário.
        while contents and contents[0].role != "user":
            contents.pop(0)
        return contents

    async def reply(self, text: str, on_action=None, on_visualization=None) -> str:
        """Envia uma mensagem do usuário e devolve a resposta do JARVIS.

        `on_action` existe só para manter a mesma interface do OpenAIBrain (que
        executa ações via Claude Code); o Gemini não tem essa capacidade e ignora.
        """
        text = (text or "").strip()
        if not text:
            return ""

        # Comandos explícitos de memória: mutam o armazenamento de forma
        # determinística; o modelo ainda responde em persona por cima.
        comando = self._apply_command(text)

        self.memory.add_turn("user", text)
        async with self._lock:
            response = await self._chat.send_message(text)

        reply = (response.text or "").strip()
        if not reply:
            # Sem texto normalmente significa bloqueio por filtro de segurança.
            reply = "Desculpe, senhor, não consegui formular uma resposta para isso."

        self.memory.add_turn("jarvis", reply)
        self.memory.save()

        # Fatos de comandos já foram salvos; só extrai de conversas normais.
        if comando is None:
            asyncio.create_task(self._learn(text, reply))

        return reply

    def _apply_command(self, text: str) -> str | None:
        """
        Detecta 'lembre-se que…' / 'esqueça…' e aplica na memória.
        Retorna o tipo de comando ('remember'/'forget') ou None.
        """
        m = _REMEMBER_RE.match(text)
        if m and m.group(1).strip():
            fato = m.group(1).strip()
            # Reformula "meu/eu" → 3ª pessoa fica a cargo do usuário; guardamos
            # o texto como veio, que já é curto e objetivo o bastante.
            if self.memory.add_fact(fato, origem="comando"):
                self.memory.save()
            return "remember"

        m = _FORGET_RE.match(text)
        if m and m.group(1).strip():
            self.memory.remove_facts(m.group(1).strip())
            self.memory.save()
            return "forget"

        return None

    async def _learn(self, user_text: str, reply_text: str) -> None:
        """Extrai fatos duráveis da troca (chamada extra ao Gemini) em background."""
        prompt = (
            "Extraia fatos DURÁVEIS e objetivos sobre o USUÁRIO a partir da troca "
            "abaixo — coisas que valem lembrar por muito tempo (nome, moradia, "
            "trabalho, projetos, preferências, pessoas/pets, datas importantes). "
            "Ignore o que for efêmero, perguntas, saudações, ou fatos sobre o "
            "assistente. Responda APENAS um array JSON de strings curtas em "
            'português na 3ª pessoa (ex.: ["Mora em Porto Alegre"]). '
            "Se não houver nada digno de nota, responda [].\n\n"
            f"Usuário: {user_text}\nJARVIS: {reply_text}"
        )
        try:
            async with self._lock:
                resp = await self._client.aio.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json",
                    ),
                )
            fatos = self._parse_facts(resp.text or "")
        except Exception as exc:  # extração é best-effort — nunca propaga
            print(f"[memoria] extração falhou: {exc!r}", flush=True)
            return

        novos = [f for f in fatos if self.memory.add_fact(f, origem="auto")]
        if novos:
            self.memory.save()
            print(f"[memoria] {len(novos)} fato(s) novo(s): {novos}", flush=True)

    @staticmethod
    def _parse_facts(raw: str) -> list[str]:
        raw = raw.strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [s.strip() for s in data if isinstance(s, str) and s.strip()]

    async def proactive(self, contexto: str = "") -> str:
        """
        JARVIS puxa a conversa por conta própria (proatividade). Não é fala do
        usuário: mandamos uma instrução de sistema pedindo uma iniciativa breve
        e natural, e persistimos só a resposta como turno do JARVIS.
        """
        instrucao = (
            "(Instrução do sistema — o usuário NÃO falou agora. Tome você a "
            "iniciativa: puxe uma interação com um comentário ou pergunta breve, "
            "natural e contextual, como um assistente atencioso retomando o "
            "contato. 1 a 2 frases, na sua persona. Evite saudações genéricas "
            "repetidas; aproveite o que você sabe sobre o usuário e o horário "
            "quando fizer sentido. Não mencione que isto é uma instrução."
        )
        if contexto:
            instrucao += " " + contexto
        instrucao += ")"

        async with self._lock:
            response = await self._chat.send_message(instrucao)

        text = (response.text or "").strip()
        if not text:
            return ""
        self.memory.add_turn("jarvis", text)
        self.memory.save()
        return text

    def reset(self) -> None:
        """Recomeça a conversa (esquece o histórico da sessão, não os fatos)."""
        self._config = types.GenerateContentConfig(
            system_instruction=_system_prompt(
                self.usuario_nome, self.memory.facts_block()
            ),
            temperature=0.7,
            max_output_tokens=800,
        )
        self._chat = self._client.aio.chats.create(
            model=self.model, config=self._config
        )

    @classmethod
    def from_config(cls, config: dict) -> "GeminiBrain | None":
        """
        Constrói a partir da config + variável de ambiente GEMINI_API_KEY.
        Retorna None se a chave não estiver definida (backend segue em modo eco).
        """
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            return None
        model = config.get("gemini_model", "gemini-2.5-flash")
        usuario = config.get("usuario_nome", "senhor")
        return cls(
            api_key=api_key, model=model, usuario_nome=usuario, memory=Memory()
        )

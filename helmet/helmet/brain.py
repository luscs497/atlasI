"""
Ponte com o cérebro do JARVIS (o MESMO do desktop: persona, ferramentas,
tarefas, finanças, hábitos, segundo cérebro, oráculo, Perplexity…).

O pacote `jarvis` é usado como biblioteca, sem alterar nada nele. Diferenças do
capacete em relação ao desktop:

- **Histórico** (contexto da conversa) vem do banco do capacete, por conversa —
  não do `memoria.json` do desktop, que não é tocado.
- **Fatos** sobre o usuário são COMPARTILHADOS: lidos do `memoria.json` e os
  novos são mesclados de volta (sem sobrescrever o histórico do desktop).
- **Persona** ganha um adendo de CANAL: voz (respostas faladas, curtas, sem
  markdown) ou, se o capacete tem display e não tem alto-falante, display (UMA
  frase que cabe na tela). Ver `persona_addendum`.
- Sem tela de visualizações nem troca de voz (não há tela no capacete). Claude
  Code só se `HELMET_ALLOW_CODE=true`.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime
from contextlib import contextmanager
from typing import Awaitable, Callable, Protocol

from helmet import jarvis_path  # noqa: F401
from jarvis.config import MEMORY_PATH, load_config
from jarvis.memory import Memory, _normalizar

log = logging.getLogger("helmet.cerebro")

HELMET_PERSONA = (
    "\n\nCANAL ATUAL: você está falando pelo CAPACETE do senhor (microfone no "
    "capacete, resposta em VOZ). Responda como fala natural: curta (1 a 2 frases, "
    "salvo pedido de detalhe), sem markdown, sem listas, sem emojis, sem URLs, "
    "números por extenso quando soar melhor. Não há tela: nada de 'veja abaixo' "
    "nem visualizações. A transcrição pode ter erros de reconhecimento de voz — "
    "interprete com bom senso e, se o pedido for ambíguo, pergunte em poucas palavras."
)

DISPLAY_ERROR = "Não consegui."

# Capacete em inglês: o prompt-base do desktop é em português, então o adendo
# deixa claro que a RESPOSTA é sempre em inglês britânico, no estilo do filme.
_EN_STYLE = (
    "LANGUAGE: ALWAYS reply in ENGLISH (British), in the style of JARVIS from Iron "
    "Man: polite, composed, a touch of dry wit. This holds even though the rest of "
    "these instructions, your tools and the stored data are in Portuguese — "
    "translate whatever you report. Carry out requests directly with your tools — "
    "never ask for confirmation for simple actions (adding a task or reminder, "
    "logging something): just do it and confirm briefly. "
)

HELMET_PERSONA_EN = (
    "\n\nCURRENT CHANNEL: you are speaking through sir's HELMET (microphone in the "
    "helmet, reply by VOICE). " + _EN_STYLE +
    "Speak naturally and briefly (1 to 2 sentences unless asked for detail), no "
    "markdown, no lists, no emojis, no URLs. There is no screen: never say 'see "
    "below' and no visualisations. The transcription may contain speech recognition "
    "errors — use common sense and, if the request is ambiguous, ask in a few words."
)


def display_persona_en(cols: int, rows: int, limit: int) -> str:
    return (
        f"\n\nCURRENT CHANNEL: you are replying through sir's HELMET. The reply is "
        f"shown on a tiny {cols}x{rows} character DISPLAY (no voice). " + _EN_STYLE +
        f"Reply with ONE sentence of at MOST {limit} characters — count them. No "
        "lists, no markdown, no emoji, no URLs, no 'sir' and no follow-up question. "
        "Numbers and times in digits (14:30). Very short confirmations: greeting -> "
        "'Hello!'; 'are you there?' -> 'I'm here.'; message sent -> 'Sent.'; action "
        "completed -> 'Done.'; if something fails -> 'Couldn't do it.'. Use your tools "
        "normally; only the FINAL reply is short. The transcription may contain speech "
        "recognition errors — use common sense."
    )


def display_persona(cols: int, rows: int, limit: int) -> str:
    return (
        f"\n\nCANAL ATUAL: você está respondendo pelo CAPACETE do senhor. A resposta "
        f"aparece num DISPLAY minúsculo de {cols}x{rows} caracteres (não há voz). "
        f"Responda com UMA frase de no MÁXIMO {limit} caracteres — conte. Sem listas, "
        "sem markdown, sem emoji, sem URLs, sem vocativo ('senhor') e sem pergunta de "
        "seguimento ('mais alguma coisa?'). Números e horas em algarismos (14:30). "
        "Confirmações bem curtas: cumprimento -> 'Oi!'; 'está aí?' -> 'Estou aqui.'; "
        "mensagem enviada -> 'Enviado.'; ação concluída -> 'Feito.'; se algo falhar -> "
        "'Não consegui.'. Use as ferramentas normalmente; só a resposta FINAL é curta. "
        "A transcrição pode ter erros de reconhecimento de voz — interprete com bom senso."
    )


_DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
         "sexta-feira", "sábado", "domingo"]


def clock_line(now: datetime | None = None) -> str:
    """
    O cérebro do desktop não recebe a hora no prompt (e não tem ferramenta de
    relógio): sem isto, "que horas são?" pelo capacete vira "não consegui".
    """
    now = now or datetime.now().astimezone()
    return (f"\nAGORA: {_DIAS[now.weekday()]}, {now:%d/%m/%Y}, {now:%H:%M} "
            "(horário local do senhor).")


def persona_addendum(display, speaker: bool, cap: int, now: datetime | None = None,  # noqa: ANN001
                     lang: str = "pt") -> str:
    """
    Escolhe o adendo de canal: display só quando o capacete TEM display e NÃO tem
    alto-falante (com alto-falante a resposta é falada, então vale o de voz).
    `display` é um `helmet.display.DisplaySpec` (ou None). Inclui a hora atual.
    """
    if display is not None and not speaker:
        from helmet.display import max_chars

        persona = display_persona_en if lang == "en" else display_persona
        base = persona(display.cols, display.rows, max_chars(display, cap))
    else:
        base = HELMET_PERSONA_EN if lang == "en" else HELMET_PERSONA
    return base + clock_line(now)


class Brain(Protocol):
    async def reply(self, text: str, on_action: Callable[[dict], Awaitable[None]] | None = None) -> str: ...
    def load_context(self, pairs: list[tuple[str, str]]) -> None: ...


class SharedFactsMemory(Memory):
    """
    `Memory` do JARVIS com histórico só em RAM e fatos mesclados no arquivo
    compartilhado (com trava), para conviver com o processo do desktop.
    """

    def __init__(self, path=MEMORY_PATH) -> None:  # noqa: ANN001
        super().__init__(path)
        self.historico = []  # o contexto do capacete vem do banco dele
        self._added: dict[str, dict] = {}
        self._removed: set[str] = set()

    def add_fact(self, texto: str, origem: str = "auto") -> bool:
        novo = super().add_fact(texto, origem)
        if novo:
            f = self.fatos[-1]
            self._added[_normalizar(f["texto"])] = f
        return novo

    def remove_facts(self, termo: str) -> list[str]:
        removidos = super().remove_facts(termo)
        for t in removidos:
            k = _normalizar(t)
            self._removed.add(k)
            self._added.pop(k, None)
        return removidos

    @contextmanager
    def _locked(self):
        lock_path = self.path.with_suffix(".capacete.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def save(self) -> None:
        if not self._added and not self._removed:
            return  # só mudou o histórico (em RAM) — nada a gravar
        with self._locked():
            data: dict = {}
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8")) or {}
                except (json.JSONDecodeError, OSError):
                    log.warning("memoria.json ilegível — não vou sobrescrever")
                    return
            fatos = [
                f for f in data.get("fatos") or []
                if _normalizar(f.get("texto", "")) not in self._removed
            ]
            chaves = {_normalizar(f.get("texto", "")) for f in fatos}
            fatos += [f for k, f in self._added.items() if k not in chaves]
            data["fatos"] = fatos
            data.setdefault("historico", [])
            tmp = self.path.with_suffix(".json.capacete.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            self.fatos = fatos
            self._added.clear()
            self._removed.clear()


class JarvisBrain:
    """Adaptador fino sobre o `OpenAIBrain` do desktop."""

    def __init__(self, inner, api_key: str) -> None:  # noqa: ANN001
        import httpx
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient

        self._inner = inner
        self._base_system = inner._system
        self.channel = HELMET_PERSONA
        inner._system = self._base_system + self.channel
        # O httpx descarta conexões ociosas em 5 s; aí cada pedido paga um novo
        # handshake TLS (~0,5–1 s). Mantemos a conexão viva por mais tempo e a
        # aquecemos (warmup) assim que o usuário começa a falar.
        inner._client = AsyncOpenAI(
            api_key=api_key,
            # Um pedido travado na OpenAI (visto: 29 s) não pode segurar o capacete:
            # corta em 20 s e tenta 1x de novo (a 2ª costuma voltar em ~1 s).
            timeout=20.0,
            max_retries=1,
            http_client=DefaultAsyncHttpxClient(
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10,
                                    keepalive_expiry=120),
            ),
        )

    def set_channel(self, addendum: str) -> None:
        """Troca o adendo de canal (voz/display) sem perder o histórico."""
        if addendum != self.channel:
            self.channel = addendum
            self._inner._system = self._base_system + addendum

    async def warmup(self) -> None:
        try:
            await self._inner._client.models.retrieve(self._inner.model)
        except Exception as exc:  # aquecimento é best-effort
            log.debug("warmup falhou: %r", exc)

    async def reply(self, text: str, on_action=None) -> str:  # noqa: ANN001
        return await self._inner.reply(text, on_action=on_action)

    def load_context(self, pairs: list[tuple[str, str]]) -> None:
        msgs: list[dict] = []
        for user, assistant in pairs:
            msgs.append({"role": "user", "content": user})
            msgs.append({"role": "assistant", "content": assistant})
        self._inner._messages = msgs


class EchoBrain:
    """Sem OPENAI_API_KEY: responde algo útil em vez de travar."""

    async def reply(self, text: str, on_action=None) -> str:  # noqa: ANN001
        return f"Ouvi: {text}. Mas estou sem cérebro, senhor; falta a chave da OpenAI."

    def load_context(self, pairs: list[tuple[str, str]]) -> None:
        pass

    async def warmup(self) -> None:
        pass


def build_brain(allow_code: bool, model: str = "") -> Brain:
    """Monta o cérebro com as mesmas ferramentas do desktop (menos as de tela)."""
    from jarvis.oraculo import Oracle
    from jarvis.openai_brain import OpenAIBrain
    from jarvis.perplexity import Perplexity

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        log.warning("OPENAI_API_KEY ausente — cérebro em modo eco")
        return EchoBrain()

    config = load_config()

    def _try(flag: str, factory):  # noqa: ANN001
        if not config.get(flag, True):
            return None
        try:
            return factory()
        except Exception as exc:  # recurso indisponível não derruba o capacete
            log.warning("%s indisponível: %r", flag, exc)
            return None

    from jarvis.financas import FinanceStore
    from jarvis.habitos import HabitStore
    from jarvis.segundo_cerebro import SecondBrain
    from jarvis.tasks import TaskStore
    from jarvis.timetrack import TimeTracker

    coder = None
    if allow_code and config.get("claude_code_enabled", True):
        from jarvis.claude_code import ClaudeCoder

        coder = ClaudeCoder.from_config(config)

    inner = OpenAIBrain(
        api_key=os.environ["OPENAI_API_KEY"].strip(),
        model=model or config.get("openai_model", "gpt-4o-mini"),
        usuario_nome=config.get("usuario_nome", "senhor"),
        memory=SharedFactsMemory(),
        coder=coder,
        tasks=_try("tasks_enabled", TaskStore),
        timer=_try("time_tracking_enabled", TimeTracker),
        finance=_try("finance_enabled", FinanceStore),
        habits=_try("habits_enabled", HabitStore),
        second_brain=_try("second_brain_enabled", lambda: SecondBrain(embedder=_embedder(config))),
        viz=None,
        oracle=Oracle.from_config(config) if config.get("oraculo_enabled", True) else None,
        realtime=Perplexity.from_config(config) if config.get("perplexity_enabled", True) else None,
        voice=None,
        max_tool_rounds=int(config.get("claude_code_max_rounds", 3)),
    )
    return JarvisBrain(inner, os.environ["OPENAI_API_KEY"].strip())


def _embedder(config: dict):
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"].strip())
    model = config.get("embedding_model", "text-embedding-3-small")

    async def embed(texto: str) -> list[float]:
        resp = await client.embeddings.create(model=model, input=texto)
        return resp.data[0].embedding

    return embed

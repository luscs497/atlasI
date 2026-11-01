"""
Regras de negócio das conversas do capacete.

1. Toda interação (fala → resposta) é gravada na conversa ABERTA do dispositivo.
2. Uma conversa atravessa várias escutas ("Atlas" … 2 min de AFK) até o seu
   tamanho (`size_bytes`, texto UTF-8) atingir `conversation_max_bytes`.
3. O limite NUNCA corta uma escuta ao vivo: se estourar no meio, a conversa
   continua a mesma e só é encerrada quando a escuta terminar.
4. Conversa encerrada vai para a OpenAI gerar título + descrição curtos
   (em segundo plano, com novas tentativas se falhar).
5. A próxima escuta depois do encerramento abre uma conversa nova.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helmet.db import Conversation, Device, Interaction, ListenSession
from helmet.timeutil import utcnow

log = logging.getLogger("helmet.conversas")

# (transcrição) -> (título, descrição)
Summarizer = Callable[[str], Awaitable[tuple[str, str]]]
EventSink = Callable[[dict], None]


@dataclass
class OpenedSession:
    conversation_id: int
    session_id: int
    new_conversation: bool


@dataclass
class Metrics:
    audio_ms: int | None = None
    stt_ms: int | None = None
    brain_ms: int | None = None
    tts_first_ms: int | None = None
    total_ms: int | None = None
    error: str | None = None


def text_size(*parts: str) -> int:
    return sum(len((p or "").encode("utf-8")) for p in parts)


class ConversationService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        max_bytes: int,
        summarizer: Summarizer | None,
        max_summary_attempts: int = 3,
        on_event: EventSink | None = None,
    ) -> None:
        self._sm = sessionmaker
        self.max_bytes = max_bytes
        self._summarizer = summarizer
        self._max_attempts = max_summary_attempts
        self._on_event = on_event or (lambda e: None)
        self._tasks: set[asyncio.Task] = set()

    # ---- dispositivos -----------------------------------------------------

    async def touch_device(
        self, device_id: str, firmware: str | None = None, ip: str | None = None
    ) -> None:
        async with self._sm.begin() as db:
            dev = await db.get(Device, device_id)
            if dev is None:
                dev = Device(id=device_id)
                db.add(dev)
            if firmware:
                dev.firmware = firmware
            if ip:
                dev.last_ip = ip
            dev.last_seen_at = utcnow()

    # ---- escutas ----------------------------------------------------------

    async def open_session(self, device_id: str, wake_text: str | None) -> OpenedSession:
        """Abre uma escuta na conversa aberta do dispositivo (ou numa nova)."""
        await self.touch_device(device_id)
        stale = None
        async with self._sm.begin() as db:
            conv = await self._open_conversation(db, device_id)
            # Já cheia (ex.: limite reduzido na config) — encerra antes de seguir.
            if conv is not None and conv.size_bytes >= self.max_bytes:
                self._mark_closed(conv, "limit")
                stale, conv = conv.id, None
            new = conv is None
            if conv is None:
                conv = Conversation(device_id=device_id)
                db.add(conv)
                await db.flush()
            conv.session_count += 1
            conv.last_activity_at = utcnow()
            sess = ListenSession(
                conversation_id=conv.id, device_id=device_id, wake_text=wake_text
            )
            db.add(sess)
            await db.flush()
            opened = OpenedSession(conv.id, sess.id, new)
        if stale is not None:
            self._on_event({"type": "conversation_closed", "conversation_id": stale})
            self.schedule_summary(stale)
        log.info(
            "escuta %s aberta (conversa %s%s)",
            opened.session_id, opened.conversation_id, ", NOVA" if new else "",
        )
        return opened

    async def close_session(self, session_id: int, reason: str) -> int | None:
        """
        Encerra a escuta. Se a conversa já passou do limite, encerra a conversa
        também e agenda o resumo. Devolve o id da conversa encerrada (ou None).
        """
        async with self._sm.begin() as db:
            sess = await db.get(ListenSession, session_id)
            if sess is None or sess.ended_at is not None:
                return None
            sess.ended_at = utcnow()
            sess.end_reason = reason
            conv = await db.get(Conversation, sess.conversation_id)
            closed = None
            if conv.status == "open" and conv.size_bytes >= self.max_bytes:
                self._mark_closed(conv, "limit")
                closed = conv.id
        log.info("escuta %s encerrada (%s)", session_id, reason)
        if closed is not None:
            log.info("conversa %s atingiu o limite e foi encerrada", closed)
            self._on_event({"type": "conversation_closed", "conversation_id": closed})
            self.schedule_summary(closed)
        return closed

    # ---- interações -------------------------------------------------------

    async def record_interaction(
        self,
        session_id: int,
        user_text: str,
        assistant_text: str,
        source: str = "voice",
        metrics: Metrics | None = None,
    ) -> Interaction:
        """Grava a troca. Nunca encerra a conversa (mesmo acima do limite)."""
        m = metrics or Metrics()
        size = text_size(user_text, assistant_text)
        async with self._sm.begin() as db:
            sess = await db.get(ListenSession, session_id)
            if sess is None:
                raise ValueError(f"escuta {session_id} inexistente")
            it = Interaction(
                conversation_id=sess.conversation_id,
                session_id=session_id,
                source=source,
                user_text=user_text,
                assistant_text=assistant_text,
                size_bytes=size,
                audio_ms=m.audio_ms,
                stt_ms=m.stt_ms,
                brain_ms=m.brain_ms,
                tts_first_ms=m.tts_first_ms,
                total_ms=m.total_ms,
                error=m.error,
            )
            db.add(it)
            await db.execute(
                update(Conversation)
                .where(Conversation.id == sess.conversation_id)
                .values(
                    size_bytes=Conversation.size_bytes + size,
                    interaction_count=Conversation.interaction_count + 1,
                    last_activity_at=utcnow(),
                )
            )
        return it

    async def recent_context(self, conversation_id: int, limit: int) -> list[tuple[str, str]]:
        """Últimas `limit` trocas (em ordem cronológica) p/ recarregar no cérebro."""
        async with self._sm() as db:
            rows = (
                await db.execute(
                    select(Interaction.user_text, Interaction.assistant_text)
                    .where(Interaction.conversation_id == conversation_id)
                    .order_by(Interaction.id.desc())
                    .limit(limit)
                )
            ).all()
        return [(u, a) for u, a in reversed(rows)]

    # ---- encerramento / resumo --------------------------------------------

    async def finalize(self, conversation_id: int, reason: str = "manual") -> bool:
        """Encerra manualmente. Recusa se houver escuta ao vivo nessa conversa."""
        async with self._sm.begin() as db:
            conv = await db.get(Conversation, conversation_id)
            if conv is None:
                raise KeyError(conversation_id)
            if conv.status != "open":
                return False
            live = await db.scalar(
                select(func.count()).select_from(ListenSession).where(
                    ListenSession.conversation_id == conversation_id,
                    ListenSession.ended_at.is_(None),
                )
            )
            if live:
                raise RuntimeError("conversa com escuta ao vivo")
            self._mark_closed(conv, reason)
        self._on_event({"type": "conversation_closed", "conversation_id": conversation_id})
        self.schedule_summary(conversation_id)
        return True

    @staticmethod
    def _mark_closed(conv: Conversation, reason: str) -> None:
        conv.status = "closed"
        conv.ended_at = utcnow()
        conv.close_reason = reason
        conv.summary_status = "pending"

    def schedule_summary(self, conversation_id: int) -> None:
        if self._summarizer is None:
            return
        task = asyncio.create_task(self.summarize(conversation_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def summarize(self, conversation_id: int) -> bool:
        """Gera título/descrição da conversa encerrada. True se deu certo."""
        if self._summarizer is None:
            return False
        transcript = await self.transcript(conversation_id)
        try:
            if not transcript.strip():
                title, desc = "Conversa vazia", "Nenhuma interação registrada."
            else:
                title, desc = await self._summarizer(transcript)
            status, err = "done", None
        except Exception as exc:  # rede/cota — tenta de novo depois
            log.warning("resumo da conversa %s falhou: %r", conversation_id, exc)
            title = desc = None
            status, err = "failed", repr(exc)[:500]
        async with self._sm.begin() as db:
            conv = await db.get(Conversation, conversation_id)
            conv.summary_attempts += 1
            conv.summary_status = status
            conv.summary_error = err
            if status == "done":
                conv.title, conv.description = title, desc
        if status == "done":
            log.info("conversa %s resumida: %s", conversation_id, title)
            self._on_event(
                {"type": "conversation_summarized", "conversation_id": conversation_id,
                 "title": title, "description": desc}
            )
        return status == "done"

    async def transcript(self, conversation_id: int) -> str:
        async with self._sm() as db:
            rows = (
                await db.execute(
                    select(Interaction.user_text, Interaction.assistant_text)
                    .where(Interaction.conversation_id == conversation_id)
                    .order_by(Interaction.id)
                )
            ).all()
        return "\n".join(f"Usuário: {u}\nATLAS: {a}" for u, a in rows)

    # ---- boot -------------------------------------------------------------

    async def recover(self) -> None:
        """
        Pós-queda/reinício: fecha escutas que ficaram abertas, aplica o limite
        nas conversas e retoma resumos pendentes/falhos.
        """
        async with self._sm() as db:
            dangling = (
                await db.scalars(
                    select(ListenSession.id).where(ListenSession.ended_at.is_(None))
                )
            ).all()
        for sid in dangling:
            await self.close_session(sid, "restart")

        async with self._sm() as db:
            pending = (
                await db.scalars(
                    select(Conversation.id).where(
                        Conversation.status == "closed",
                        Conversation.summary_status.in_(("pending", "failed")),
                        Conversation.summary_attempts < self._max_attempts,
                    )
                )
            ).all()
        for cid in pending:
            self.schedule_summary(cid)
        if dangling or pending:
            log.info(
                "recuperação: %d escuta(s) fechada(s), %d resumo(s) retomado(s)",
                len(dangling), len(pending),
            )

    async def drain(self, timeout: float = 10.0) -> None:
        """Aguarda os resumos em andamento (desligamento/testes)."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    async def _open_conversation(db: AsyncSession, device_id: str) -> Conversation | None:
        return await db.scalar(
            select(Conversation)
            .where(Conversation.device_id == device_id, Conversation.status == "open")
            .order_by(Conversation.id.desc())
            .limit(1)
        )

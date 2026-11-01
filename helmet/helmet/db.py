"""
Modelo de dados do capacete (SQLAlchemy 2.0, async).

    Device ──< Conversation ──< ListenSession ──< Interaction
                     └────────────────────────────────┘

- **Device**: cada capacete/ESP32 que já se conectou.
- **Conversation**: agrupa as interações até estourar `conversation_max_bytes`.
  Atravessa várias escutas; quando é encerrada, ganha título e descrição (OpenAI).
- **ListenSession**: uma janela de escuta ativa — começa no "Atlas" e termina
  após o AFK (ou comando/desligamento).
- **Interaction**: uma troca (fala do usuário → resposta do ATLAS) + métricas.

Banco separado do `atlas.db` do desktop (fica em `~/.atlas/capacete.db`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from helmet.timeutil import utcnow


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    firmware: Mapped[str | None] = mapped_column(String(32))
    last_ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    # open -> recebe interações | closed -> encerrada (limite atingido/manual)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(default=utcnow)
    ended_at: Mapped[datetime | None]
    close_reason: Mapped[str | None] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(default=0)
    interaction_count: Mapped[int] = mapped_column(default=0)
    session_count: Mapped[int] = mapped_column(default=0)
    title: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # none -> pending -> done | failed
    summary_status: Mapped[str] = mapped_column(String(16), default="none")
    summary_attempts: Mapped[int] = mapped_column(default=0)
    summary_error: Mapped[str | None] = mapped_column(Text)

    sessions: Mapped[list["ListenSession"]] = relationship(
        back_populates="conversation", order_by="ListenSession.id"
    )


class ListenSession(Base):
    __tablename__ = "listen_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    ended_at: Mapped[datetime | None]
    # timeout | command | manual | restart | shutdown
    end_reason: Mapped[str | None] = mapped_column(String(32))
    wake_text: Mapped[str | None] = mapped_column(Text)

    conversation: Mapped[Conversation] = relationship(back_populates="sessions")


class Interaction(Base):
    __tablename__ = "interactions"
    __table_args__ = (Index("ix_interactions_conv_id", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("listen_sessions.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    source: Mapped[str] = mapped_column(String(16), default="voice")  # voice | text
    user_text: Mapped[str] = mapped_column(Text)
    assistant_text: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(default=0)
    audio_ms: Mapped[int | None]  # duração da fala do usuário
    stt_ms: Mapped[int | None]
    brain_ms: Mapped[int | None]
    tts_first_ms: Mapped[int | None]  # síntese até o 1º pedaço de áudio
    total_ms: Mapped[int | None]  # fim da fala -> 1º áudio de resposta
    error: Mapped[str | None] = mapped_column(Text)


def make_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url)
    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def _pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


async def init_db(engine: AsyncEngine) -> None:
    if engine.url.get_backend_name() == "sqlite" and engine.url.database:
        from pathlib import Path

        Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)

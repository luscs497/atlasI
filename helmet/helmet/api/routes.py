"""Rotas: WebSocket do capacete, WebSocket de monitoramento e API REST."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from helmet import __version__
from helmet.db import Conversation, Interaction, ListenSession
from helmet.runtime import Hub
from helmet.timeutil import iso

log = logging.getLogger("helmet.api")
router = APIRouter()


# ---- auth -----------------------------------------------------------------

def _token_ok(expected: str, given: str | None) -> bool:
    if not expected:
        return True  # sem token configurado = rede local de dev
    return bool(given) and hmac.compare_digest(expected, given)


def _extract_token(headers, query_token: str | None) -> str | None:  # noqa: ANN001
    if query_token:
        return query_token
    auth = headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def hub_of(request: Request) -> Hub:
    return request.app.state.hub


def require_auth(request: Request, token: str | None = Query(None)) -> None:
    hub = hub_of(request)
    if not _token_ok(hub.settings.token, _extract_token(request.headers, token)):
        raise HTTPException(401, "token inválido")


# ---- WebSocket do capacete -------------------------------------------------

class WsLink:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws

    async def send_json(self, data: dict) -> None:
        await self.ws.send_text(json.dumps(data, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def close(self, code: int = 1000) -> None:
        await self.ws.close(code=code)


@router.websocket("/ws/device")
async def device_ws(
    ws: WebSocket,
    device_id: str = Query("capacete", min_length=1, max_length=64),
    token: str | None = Query(None),
) -> None:
    hub: Hub = ws.app.state.hub
    if not _token_ok(hub.settings.token, _extract_token(ws.headers, token)):
        await ws.close(code=4401)
        return
    await ws.accept()
    rt = hub.get(device_id)
    link = WsLink(ws)
    ip = ws.client.host if ws.client else None
    log.info("capacete '%s' conectado de %s", device_id, ip)
    await rt.attach(link, ip=ip)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                rt.feed_audio(msg["bytes"])
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                await link.send_json({"type": "error", "error": "JSON inválido"})
                continue
            await _device_control(rt, link, data)
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:  # socket já fechado (conexão substituída)
        log.debug("ws %s: %r", device_id, exc)
    finally:
        rt.detach(link)
        log.info("capacete '%s' desconectado", device_id)


async def _device_control(rt, link: WsLink, data: dict) -> None:  # noqa: ANN001
    kind = data.get("type")
    if kind == "hello":
        await rt.configure(data)
    elif kind == "ping":
        await link.send_json({"type": "pong", "t": data.get("t")})
    elif kind == "playback_done":
        rt.playback_done(int(data.get("seq", 0)))
    elif kind == "wake":  # botão físico no capacete: ativa sem dizer "Jarvis"
        await rt.activate(None, reason="button")
    elif kind == "sleep":
        await rt.deactivate("button")
    elif kind == "text":  # firmware/teclado mandando texto direto
        asyncio.create_task(rt.submit_text(str(data.get("text", "")), force_active=True))
    else:
        await link.send_json({"type": "error", "error": f"tipo desconhecido: {kind!r}"})


# ---- monitor (eventos ao vivo) ---------------------------------------------

@router.websocket("/ws/monitor")
async def monitor_ws(ws: WebSocket, token: str | None = Query(None)) -> None:
    hub: Hub = ws.app.state.hub
    if not _token_ok(hub.settings.token, _extract_token(ws.headers, token)):
        await ws.close(code=4401)
        return
    await ws.accept()
    q = hub.events.subscribe()
    try:
        await ws.send_text(json.dumps(
            {"type": "snapshot", "devices": [d.snapshot() for d in hub.devices.values()]},
            ensure_ascii=False,
        ))
        while True:
            ev = await q.get()
            await ws.send_text(json.dumps(ev, ensure_ascii=False, default=str))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.events.unsubscribe(q)


# ---- REST -----------------------------------------------------------------

@router.get("/health")
async def health(request: Request) -> dict:
    hub = hub_of(request)
    m = hub.models
    return {
        "status": "ok",
        "version": __version__,
        "stt_ready": m.stt is not None,
        "tts_ready": m.tts is not None,
        "tts_voice": getattr(m.tts, "label", None),
        "models_error": m.error,
        "devices_connected": sum(1 for d in hub.devices.values() if d.link),
    }


@router.get("/api/devices", dependencies=[Depends(require_auth)])
async def list_devices(request: Request) -> list[dict]:
    return [d.snapshot() for d in hub_of(request).devices.values()]


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    # true = trata como se o capacete já estivesse ativo (dispensa o "Jarvis")
    force_active: bool = False


@router.post("/api/devices/{device_id}/text", dependencies=[Depends(require_auth)])
async def device_text(device_id: str, body: TextIn, request: Request) -> dict:
    return await hub_of(request).get(device_id).submit_text(body.text, body.force_active)


@router.post("/api/devices/{device_id}/wake", dependencies=[Depends(require_auth)])
async def device_wake(device_id: str, request: Request) -> dict:
    rt = hub_of(request).get(device_id)
    await rt.activate(None, reason="manual")
    return rt.snapshot()


@router.post("/api/devices/{device_id}/sleep", dependencies=[Depends(require_auth)])
async def device_sleep(device_id: str, request: Request) -> dict:
    rt = hub_of(request).get(device_id)
    await rt.deactivate("manual")
    return rt.snapshot()


def _conv_out(c: Conversation) -> dict:
    return {
        "id": c.id,
        "device_id": c.device_id,
        "status": c.status,
        "title": c.title,
        "description": c.description,
        "started_at": iso(c.started_at),
        "last_activity_at": iso(c.last_activity_at),
        "ended_at": iso(c.ended_at),
        "close_reason": c.close_reason,
        "size_bytes": c.size_bytes,
        "interaction_count": c.interaction_count,
        "session_count": c.session_count,
        "summary_status": c.summary_status,
        "summary_error": c.summary_error,
    }


@router.get("/api/conversations", dependencies=[Depends(require_auth)])
async def list_conversations(
    request: Request,
    device_id: str | None = None,
    status: str | None = Query(None, pattern="^(open|closed)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    sm = request.app.state.sessionmaker
    stmt = select(Conversation)
    count = select(func.count()).select_from(Conversation)
    if device_id:
        stmt = stmt.where(Conversation.device_id == device_id)
        count = count.where(Conversation.device_id == device_id)
    if status:
        stmt = stmt.where(Conversation.status == status)
        count = count.where(Conversation.status == status)
    async with sm() as db:
        total = await db.scalar(count)
        rows = (await db.scalars(
            stmt.order_by(Conversation.id.desc()).limit(limit).offset(offset)
        )).all()
    return {"total": total, "items": [_conv_out(c) for c in rows],
            "max_bytes": hub_of(request).settings.conversation_max_bytes}


@router.get("/api/conversations/{conv_id}", dependencies=[Depends(require_auth)])
async def get_conversation(conv_id: int, request: Request) -> dict:
    sm = request.app.state.sessionmaker
    async with sm() as db:
        c = await db.get(Conversation, conv_id)
        if c is None:
            raise HTTPException(404, "conversa não encontrada")
        sessions = (await db.scalars(
            select(ListenSession).where(ListenSession.conversation_id == conv_id)
            .order_by(ListenSession.id)
        )).all()
    out = _conv_out(c)
    out["sessions"] = [
        {"id": s.id, "started_at": iso(s.started_at), "ended_at": iso(s.ended_at),
         "end_reason": s.end_reason, "wake_text": s.wake_text}
        for s in sessions
    ]
    return out


@router.get("/api/conversations/{conv_id}/interactions", dependencies=[Depends(require_auth)])
async def get_interactions(
    conv_id: int, request: Request,
    limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0),
) -> list[dict]:
    sm = request.app.state.sessionmaker
    async with sm() as db:
        rows = (await db.scalars(
            select(Interaction).where(Interaction.conversation_id == conv_id)
            .order_by(Interaction.id).limit(limit).offset(offset)
        )).all()
    return [
        {"id": i.id, "session_id": i.session_id, "created_at": iso(i.created_at),
         "source": i.source, "user": i.user_text, "jarvis": i.assistant_text,
         "size_bytes": i.size_bytes, "audio_ms": i.audio_ms, "stt_ms": i.stt_ms,
         "brain_ms": i.brain_ms, "tts_first_ms": i.tts_first_ms,
         "total_ms": i.total_ms, "error": i.error}
        for i in rows
    ]


@router.post("/api/conversations/{conv_id}/finalize", dependencies=[Depends(require_auth)])
async def finalize_conversation(conv_id: int, request: Request) -> dict:
    service = hub_of(request).service
    try:
        changed = await service.finalize(conv_id)
    except KeyError:
        raise HTTPException(404, "conversa não encontrada")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"finalized": changed}


@router.post("/api/conversations/{conv_id}/summarize", dependencies=[Depends(require_auth)])
async def summarize_conversation(conv_id: int, request: Request) -> dict:
    sm = request.app.state.sessionmaker
    async with sm() as db:
        c = await db.get(Conversation, conv_id)
    if c is None:
        raise HTTPException(404, "conversa não encontrada")
    if c.status != "closed":
        raise HTTPException(409, "só conversas encerradas recebem resumo")
    ok = await hub_of(request).service.summarize(conv_id)
    return {"ok": ok}

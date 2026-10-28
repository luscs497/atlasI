"""
Fixtures: servidor uvicorn REAL numa thread (rede/WebSocket de verdade), com
STT/TTS/cérebro/resumo FALSOS e determinísticos, e banco SQLite temporário.

Áudio de teste: frames com amplitude alta = "fala", zeros = silêncio (VAD por
energia injetado). O FakeSTT devolve o texto da fila na ordem dos enunciados.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections import deque

import numpy as np
import pytest
import uvicorn
import websockets

from helmet.app import create_app
from helmet.runtime import Models
from helmet.settings import Settings

RATE = 16_000


def speech(ms: int) -> bytes:
    n = RATE * ms // 1000
    return (np.ones(n) * 3000).astype("<i2").tobytes()


def silence(ms: int) -> bytes:
    return b"\x00\x00" * (RATE * ms // 1000)


def energy_vad(frame: bytes, sr: int) -> bool:
    a = np.frombuffer(frame, dtype="<i2").astype(np.float32)
    return float(np.abs(a).mean()) > 500


class FakeSTT:
    def __init__(self) -> None:
        self.queue: deque[str] = deque()
        self.calls = 0

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = RATE) -> str:
        self.calls += 1
        return self.queue.popleft() if self.queue else ""


class FakeTTS:
    label = "fake"

    def synth_pcm(self, text: str, sample_rate: int) -> bytes:
        return b"\x01\x00" * (sample_rate // 10)  # 100 ms por frase


class FakeBrain:
    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.contexts: list[list[tuple[str, str]]] = []
        self.inputs: list[str] = []

    async def reply(self, text: str, on_action=None) -> str:  # noqa: ANN001
        self.inputs.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("cérebro fora do ar")
        return f"Resposta para: {text}. Mais alguma coisa, senhor?"

    def load_context(self, pairs: list[tuple[str, str]]) -> None:
        self.contexts.append(list(pairs))


class FakeSummarizer:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def __call__(self, transcript: str) -> tuple[str, str]:
        self.calls.append(transcript)
        if self.fail:
            raise RuntimeError("openai fora do ar")
        return "Título de teste", "Descrição de teste."


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, app, port: int, settings: Settings) -> None:  # noqa: ANN001
        self.app, self.port, self.settings = app, port, settings
        self.base = f"http://127.0.0.1:{port}"
        self.ws_base = f"ws://127.0.0.1:{port}"
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                             ws_ping_interval=None)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> "LiveServer":
        self.thread.start()
        t0 = time.time()
        while not self.server.started:
            if time.time() - t0 > 10:
                raise RuntimeError("servidor não subiu")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)

    @property
    def hub(self):  # noqa: ANN201
        return self.app.state.hub

    def ws_url(self, device_id: str = "capacete-test", token: str | None = "segredo") -> str:
        q = f"?device_id={device_id}"
        return f"{self.ws_base}/ws/device{q}" + (f"&token={token}" if token else "")


@pytest.fixture
def make_server(tmp_path):
    servers: list[LiveServer] = []

    def _make(
        brain: FakeBrain | None = None,
        summarizer: FakeSummarizer | None = None,
        db_name: str = "capacete.db",
        **overrides,
    ):
        cfg = dict(
            token="segredo",
            database_url=f"sqlite+aiosqlite:///{tmp_path / db_name}",
            active_timeout_s=0.8,
            conversation_max_bytes=100_000,
            mdns_enabled=False,
            preload_models=False,
            reply_audio="none",
            vad_end_silence_ms=210,
            vad_start_ms=60,
            min_utterance_ms=100,
            stream_lead_ms=200,
            input_highpass_hz=0,  # "fala" sintética é DC: o passa-alta a zeraria
            vad_noise_factor=0,  # piso adaptativo tem teste próprio (test_units)
            language="pt",  # independe do HELMET_LANGUAGE do .env
        )
        cfg.update(overrides)
        settings = Settings(**cfg)
        stt = FakeSTT()
        models = Models(stt=stt, stt_wake=stt, tts=FakeTTS())
        brain = brain or FakeBrain()
        summ = summarizer or FakeSummarizer()
        app = create_app(settings, models=models, brain_factory=lambda: brain,
                         summarizer=summ, vad_classifier=energy_vad)
        srv = LiveServer(app, _free_port(), settings).start()
        srv.stt, srv.brain, srv.summarizer = stt, brain, summ
        servers.append(srv)
        return srv

    yield _make
    for s in servers:
        s.stop()


class DeviceClient:
    """Capacete simulado: manda áudio, coleta JSON e binários recebidos."""

    def __init__(self, ws) -> None:  # noqa: ANN001
        self.ws = ws
        self.messages: list[dict] = []
        self.audio = bytearray()
        self._task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    self.audio.extend(raw)
                else:
                    self.messages.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    async def say(self, ms: int = 400) -> None:
        """Um enunciado: fala + silêncio suficiente para fechar (em tempo ~real)."""
        await self.stream(speech(ms) + silence(400))

    async def stream(self, pcm: bytes, chunk_ms: int = 40) -> None:
        step = RATE * 2 * chunk_ms // 1000
        for i in range(0, len(pcm), step):
            await self.ws.send(pcm[i:i + step])
            await asyncio.sleep(0.002)

    def of(self, msg_type: str, **match) -> list[dict]:
        return [m for m in self.messages if m.get("type") == msg_type
                and all(m.get(k) == v for k, v in match.items())]

    async def wait_for(self, msg_type: str, timeout: float = 5.0, count: int = 1, **match) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            found = self.of(msg_type, **match)
            if len(found) >= count:
                return found[count - 1]
            await asyncio.sleep(0.02)
        raise AssertionError(f"não chegou {msg_type} {match} (x{count}); recebidas: "
                             f"{[m.get('type') + ':' + str(m.get('name') or m.get('state') or '') for m in self.messages]}")

    async def close(self) -> None:
        await self.ws.close()
        await self._task


@pytest.fixture
async def connect():
    clients: list[DeviceClient] = []

    async def _connect(url: str, hello: dict | None = None) -> DeviceClient:
        ws = await websockets.connect(url, max_size=None)
        c = DeviceClient(ws)
        if hello is not None:
            await ws.send(json.dumps({"type": "hello", **hello}))
        clients.append(c)
        await c.wait_for("welcome")
        return c

    yield _connect
    for c in clients:
        try:
            await c.close()
        except Exception:
            pass

"""
ATLAS I — servidor backend (WebSocket local).

Passo 1: estabelece o canal de comunicação com a UI Electron via WebSocket
em 127.0.0.1.
Passo 3: dá "cérebro" ao backend — as mensagens do usuário vão para a API do
Gemini (`atlas.gemini.GeminiBrain`). Se não houver GEMINI_API_KEY, o backend
volta ao modo eco (placeholder).
Passo 4: voz — STT (faster-whisper) transcreve o áudio gravado pela UI e TTS
(Piper) devolve a resposta falada. Modelos carregam em segundo plano após o
boot para não atrasar a UI.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import random
import signal
import time
from datetime import datetime
from pathlib import Path

import websockets
from dotenv import load_dotenv
from websockets.asyncio.server import ServerConnection

from atlas import __version__
from atlas.config import load_config, save_config
from atlas.gemini import GeminiBrain
from atlas.openai_brain import OpenAIBrain
from atlas.elevenlabs_tts import ElevenSpeaker
from atlas.kokoro_tts import KokoroSpeaker
from atlas.claude_code import ClaudeCoder
from atlas.oraculo import Oracle
from atlas.perplexity import Perplexity
from atlas.tasks import TaskStore
from atlas.timetrack import TimeTracker
from atlas.financas import FinanceStore
from atlas.habitos import HabitStore
from atlas.segundo_cerebro import SecondBrain
from atlas.visualizacoes import VizStore
from atlas.stt import Transcriber
from atlas.tts import Speaker

# Tipo do cérebro: qualquer provedor com a mesma interface (reply/proactive/...).
Brain = GeminiBrain | OpenAIBrain
# Tipo da voz: Piper (local) ou ElevenLabs (premium), mesmo `synthesize()`.
Voice = Speaker | ElevenSpeaker | KokoroSpeaker

# Carrega segredos do .env na raiz do projeto (../ a partir de backend/).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

HOST = "127.0.0.1"

# Cérebro compartilhado (uma conversa; assistente de usuário único).
BRAIN: Brain | None = None

# Voz (carregada em segundo plano no boot; None enquanto não estiver pronta).
TRANSCRIBER: Transcriber | None = None
SPEAKER: Voice | None = None

# Clientes conectados — para avisar (broadcast) quando a voz ficar disponível.
CLIENTS: set[ServerConnection] = set()

# Estado para a proatividade (assistente de usuário único → globais simples).
CURRENT_MODE = "text"        # modo atual da UI (texto/voz) — decide se ATLAS fala
BUSY = False                 # há um turno em andamento? (não interromper)
LAST_ACTIVITY = time.time()  # última interação — janela de silêncio antes de agir

# Último design de visualização exibido. Reenviado a cada nova conexão para que a
# 2ª janela (ao abrir/reabrir pelo botão) já apareça com o último design pronto.
LAST_VIZ: dict | None = None
VIZ_STORE = None  # referência ao VizStore (para persistir o último design)


def _encode(msg_type: str, payload: dict | None = None) -> str:
    return json.dumps({"type": msg_type, "payload": payload or {}})


async def _broadcast(msg: str) -> None:
    for ws in list(CLIENTS):
        try:
            await ws.send(msg)
        except Exception:
            pass


@contextlib.asynccontextmanager
async def _activity():
    """Marca um turno em andamento: bloqueia a proatividade e reinicia a janela
    de silêncio, mesmo que algo falhe no meio (finally)."""
    global BUSY, LAST_ACTIVITY
    BUSY = True
    LAST_ACTIVITY = time.time()
    try:
        yield
    finally:
        BUSY = False
        LAST_ACTIVITY = time.time()


async def handle_client(websocket: ServerConnection) -> None:
    """Ciclo de vida de uma conexão da UI."""
    peer = websocket.remote_address
    print(f"[conn] cliente conectado: {peer}", flush=True)
    CLIENTS.add(websocket)

    # Saudação inicial — a UI usa isso para confirmar que o backend está vivo.
    await websocket.send(
        _encode(
            "hello",
            {
                "version": __version__,
                "text": "Backend online. Canal WebSocket estabelecido.",
                "voice_ready": TRANSCRIBER is not None,
            },
        )
    )

    # Reenvia o último design ao novo cliente — assim a 2ª janela, ao (re)abrir
    # pelo botão, já surge com o que estava na tela (sem precisar pedir de novo).
    if LAST_VIZ is not None:
        await websocket.send(_encode("visualization", {"spec": LAST_VIZ}))

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[warn] payload inválido: {raw!r}", flush=True)
                continue
            await route_message(websocket, msg)
    except websockets.ConnectionClosed:
        pass
    finally:
        CLIENTS.discard(websocket)
        print(f"[conn] cliente desconectado: {peer}", flush=True)


async def route_message(websocket: ServerConnection, msg: dict) -> None:
    """Roteia mensagens recebidas da UI por tipo."""
    mtype = msg.get("type")
    payload = msg.get("payload") or {}

    if mtype == "ping":
        await websocket.send(_encode("pong", {"ts": time.time()}))

    elif mtype == "message":
        async with _activity():
            await handle_user_message(
                websocket, payload.get("text", ""), speak=bool(payload.get("speak"))
            )

    elif mtype == "audio":
        async with _activity():
            await handle_audio(
                websocket, payload.get("audio_b64", ""), speak=bool(payload.get("speak", True))
            )

    elif mtype == "mode":
        # A UI informa em que mundo está (texto/voz) — usado pela proatividade
        # para decidir se a iniciativa também sai falada.
        global CURRENT_MODE
        CURRENT_MODE = "voice" if payload.get("mode") == "voice" else "text"

    else:
        print(f"[warn] tipo desconhecido: {mtype}", flush=True)


async def handle_audio(
    websocket: ServerConnection, audio_b64: str, speak: bool
) -> None:
    """Transcreve o áudio gravado na UI (STT) e o trata como mensagem falada."""
    if TRANSCRIBER is None:
        await websocket.send(
            _encode("status", {"state": "idle", "text": "voz ainda carregando…"})
        )
        return
    if not audio_b64:
        return

    await websocket.send(
        _encode("status", {"state": "thinking", "text": "ouvindo…"})
    )
    try:
        audio = base64.b64decode(audio_b64)
        text = await asyncio.to_thread(TRANSCRIBER.transcribe, audio)
    except Exception as exc:
        print(f"[erro] falha no STT: {exc!r}", flush=True)
        await websocket.send(
            _encode("message", {"from": "atlas", "text": "Não entendi o áudio, senhor."})
        )
        return

    if not text:
        await websocket.send(
            _encode("status", {"state": "idle", "text": "não captei nada"})
        )
        return

    # Ecoa o que foi reconhecido para a UI mostrar como fala do usuário.
    await websocket.send(_encode("transcript", {"text": text}))
    await handle_user_message(websocket, text, speak=speak)


async def handle_user_message(
    websocket: ServerConnection, text: str, speak: bool = False
) -> None:
    """
    Conversa com o usuário via cérebro (Gemini). Assíncrono para nunca travar a
    UI. Sem chave de API, cai num eco de fallback. Se `speak` e a voz estiver
    pronta, também devolve a resposta em áudio (TTS).
    """
    text = (text or "").strip()
    if not text:
        return

    await websocket.send(
        _encode("status", {"state": "thinking", "text": "processando…"})
    )

    async def _on_action(info: dict) -> None:
        """Avisa a UI que uma ação de código está sendo executada (pode demorar)."""
        proj = info.get("projeto") or "base"
        await websocket.send(
            _encode(
                "status",
                {
                    "state": "thinking",
                    "text": f"executando no Claude Code ({info.get('modelo')})…",
                },
            )
        )
        await websocket.send(
            _encode(
                "message",
                {
                    "from": "atlas",
                    "text": f"Certo, senhor. Encaminhando ao Claude Code em '{proj}'…",
                    "info": True,
                },
            )
        )

    async def _on_visualization(spec: dict) -> None:
        """Guarda como último design e envia a TODAS as janelas (a 2ª desenha)."""
        global LAST_VIZ
        LAST_VIZ = spec
        if VIZ_STORE is not None:
            try:
                VIZ_STORE.set_last(spec)  # persiste para sobreviver a reinício
            except Exception:
                pass
        await _broadcast(_encode("visualization", {"spec": spec}))

    if BRAIN is None:
        reply = (
            f'Recebi: "{text}". Estou sem cérebro no momento, senhor — defina '
            "OPENAI_API_KEY no arquivo .env para me ligar à API."
        )
    else:
        try:
            reply = await BRAIN.reply(
                text, on_action=_on_action, on_visualization=_on_visualization
            )
        except Exception as exc:  # rede/quota/etc — nunca derruba o servidor
            print(f"[erro] falha no cérebro: {exc!r}", flush=True)
            reply = (
                "Tive um problema para processar isso agora, senhor. "
                "Verifique a conexão ou a chave de API e tente novamente."
            )

    await websocket.send(_encode("message", {"from": "atlas", "text": reply}))

    if speak and SPEAKER is not None:
        try:
            wav = await asyncio.to_thread(SPEAKER.synthesize, reply)
            if wav:
                await websocket.send(
                    _encode("speech", {"audio_b64": base64.b64encode(wav).decode()})
                )
        except Exception as exc:
            print(f"[erro] falha no TTS: {exc!r}", flush=True)


_DIAS = [
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
]


def _tempo_contexto() -> str:
    agora = datetime.now()
    return f"Horário atual: {agora.strftime('%H:%M')} de {_DIAS[agora.weekday()]}."


async def _do_proactive() -> None:
    """Gera uma iniciativa do ATLAS e a envia (texto e, no modo voz, falada)."""
    global BUSY, LAST_ACTIVITY
    BUSY = True
    try:
        text = await BRAIN.proactive(_tempo_contexto())
        if not text:
            return
        await _broadcast(
            _encode("message", {"from": "atlas", "text": text, "proactive": True})
        )
        if CURRENT_MODE == "voice" and SPEAKER is not None:
            try:
                wav = await asyncio.to_thread(SPEAKER.synthesize, text)
                if wav:
                    await _broadcast(
                        _encode("speech", {"audio_b64": base64.b64encode(wav).decode()})
                    )
            except Exception as exc:
                print(f"[erro] TTS proativo: {exc!r}", flush=True)
        print(f"[proativo] iniciativa: {text!r}", flush=True)
    except Exception as exc:  # nunca derruba o loop
        print(f"[erro] proatividade: {exc!r}", flush=True)
    finally:
        BUSY = False
        LAST_ACTIVITY = time.time()  # espaça as próximas iniciativas


async def proactive_loop(config: dict, stop: asyncio.Event) -> None:
    """
    De tempos em tempos (intervalo aleatório entre min e max minutos), o ATLAS
    puxa a conversa — mas só se houver cliente conectado, ele não estiver ocupado
    e o usuário estiver quieto há pelo menos `min` minutos. `min<=0` desativa.
    """
    min_m = float(config.get("proativo_intervalo_min", 0) or 0)
    max_m = float(config.get("proativo_intervalo_max", 0) or 0)
    if min_m <= 0 or max_m < min_m:
        print("[proativo] desativado (configure proativo_intervalo_min/max).", flush=True)
        return
    print(f"[proativo] ativo: iniciativa a cada {min_m:g}–{max_m:g} min.", flush=True)

    while not stop.is_set():
        espera = random.uniform(min_m, max_m) * 60
        try:
            await asyncio.wait_for(stop.wait(), timeout=espera)
            break  # encerrando
        except asyncio.TimeoutError:
            pass

        if not CLIENTS or BRAIN is None or BUSY:
            continue
        if (time.time() - LAST_ACTIVITY) < min_m * 60:
            continue  # usuário interagiu há pouco — não interrompe
        await _do_proactive()


async def _load_voice(config: dict) -> None:
    """Carrega STT (Whisper) e TTS (Piper) fora do event loop e avisa a UI."""
    global TRANSCRIBER, SPEAKER
    try:
        TRANSCRIBER = await asyncio.to_thread(Transcriber.from_config, config)
        print(f"[voz] STT pronto: whisper '{TRANSCRIBER.model_size}'", flush=True)
    except Exception as exc:
        print(f"[voz] STT indisponível: {exc!r}", flush=True)
    try:
        SPEAKER = await asyncio.to_thread(_make_speaker, config)
        if SPEAKER is None:
            print("[voz] TTS indisponível (sem ElevenLabs nem voz Piper).", flush=True)
        else:
            print(f"[voz] TTS pronto: {SPEAKER.label}", flush=True)
    except Exception as exc:
        print(f"[voz] TTS indisponível: {exc!r}", flush=True)

    await _broadcast(
        _encode("voice_ready", {"stt": TRANSCRIBER is not None, "tts": SPEAKER is not None})
    )


def _make_brain(config: dict) -> Brain | None:
    """Cria o cérebro conforme `ia_provider`. None se faltar a chave de API."""
    provider = config.get("ia_provider", "openai")
    if provider == "gemini":
        return GeminiBrain.from_config(config)
    # Padrão atual: OpenAI (gpt-4o-mini) — com a ponte para o Claude Code, que dá
    # ao ATLAS o poder de EXECUTAR tarefas de programação (ações).
    coder = ClaudeCoder.from_config(config)
    if coder is not None and coder.available():
        print(
            f"[boot] Claude Code pronto: modelo padrão {coder.model}, "
            f"base {coder.base_dir}",
            flush=True,
        )
    else:
        print("[boot] Claude Code indisponível (só conversa).", flush=True)

    # Sistema de tarefas (banco local): dá ao ATLAS gestão das listas
    # Trabalho/Pessoal. Não precisa de chave externa — só do banco em ~/.atlas.
    tasks = None
    if config.get("tasks_enabled", True):
        try:
            tasks = TaskStore()
            print("[boot] Tarefas prontas (Trabalho/Pessoal em atlas.db).", flush=True)
        except Exception as exc:  # banco indisponível não pode derrubar o boot
            print(f"[boot] Tarefas indisponíveis: {exc!r}", flush=True)

    # Rastreamento de tempo por projeto (mesmo banco local).
    timer = None
    if config.get("time_tracking_enabled", True):
        try:
            timer = TimeTracker()
            print("[boot] Rastreamento de tempo pronto (time_entries).", flush=True)
        except Exception as exc:
            print(f"[boot] Rastreamento de tempo indisponível: {exc!r}", flush=True)

    # Finanças pessoais (mesmo banco local).
    finance = None
    if config.get("finance_enabled", True):
        try:
            finance = FinanceStore()
            print("[boot] Finanças prontas (expenses).", flush=True)
        except Exception as exc:
            print(f"[boot] Finanças indisponíveis: {exc!r}", flush=True)

    # Hábitos / desenvolvimento pessoal (mesmo banco local).
    habits = None
    if config.get("habits_enabled", True):
        try:
            habits = HabitStore()
            print("[boot] Hábitos prontos (habits/habit_checkins).", flush=True)
        except Exception as exc:
            print(f"[boot] Hábitos indisponíveis: {exc!r}", flush=True)

    # Segundo cérebro (ideias conectadas por embedding). O embedder usa a OpenAI;
    # sem chave, o SecondBrain ainda funciona por palavra-chave.
    second_brain = None
    if config.get("second_brain_enabled", True):
        try:
            second_brain = SecondBrain(embedder=_make_embedder(config))
            print("[boot] Segundo cérebro pronto (ideas).", flush=True)
        except Exception as exc:
            print(f"[boot] Segundo cérebro indisponível: {exc!r}", flush=True)

    # Tela de visualizações (2ª janela): guarda padrões nomeados; o desenho é da UI.
    viz = None
    if config.get("viz_enabled", True):
        try:
            viz = VizStore()
            global VIZ_STORE, LAST_VIZ
            VIZ_STORE = viz
            LAST_VIZ = viz.get_last()  # recupera o último design entre reinícios
            print("[boot] Visualizações prontas (visualizations).", flush=True)
        except Exception as exc:
            print(f"[boot] Visualizações indisponíveis: {exc!r}", flush=True)

    # Oráculo: consulta ao Claude quando o GPT não sabe (mesma CLI do Claude Code).
    oracle = Oracle.from_config(config)
    if oracle is not None and oracle.available():
        print(f"[boot] Oráculo pronto (Claude {oracle.model}).", flush=True)
    else:
        print("[boot] Oráculo indisponível.", flush=True)

    # Perplexity Sonar: dados em tempo real (cotações/clima/notícias).
    realtime = Perplexity.from_config(config)
    if realtime is not None and realtime.available():
        print(f"[boot] Tempo real pronto (Perplexity {realtime.model}).", flush=True)
    else:
        print("[boot] Tempo real indisponível (sem PERPLEXITY_API_KEY).", flush=True)

    # Controle de voz em runtime: deixa a IA trocar motor/voz do TTS por comando.
    voice = VoiceController(config)

    return OpenAIBrain.from_config(
        config, coder=coder, tasks=tasks, timer=timer, finance=finance,
        habits=habits, second_brain=second_brain, viz=viz, oracle=oracle,
        realtime=realtime, voice=voice,
    )


def _make_embedder(config: dict):
    """Cria um embedder async via OpenAI (ou None se não houver chave)."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    model = config.get("embedding_model", "text-embedding-3-small")

    async def embed(texto: str) -> list[float]:
        resp = await client.embeddings.create(model=model, input=texto)
        return resp.data[0].embedding

    return embed


def _make_speaker(config: dict) -> Voice | None:
    """
    Cria a voz conforme `tts_engine`. Com `elevenlabs`, usa a voz premium e mantém
    o Piper local como fallback (para a cota grátis acabar sem deixar o ATLAS
    mudo). Sem chave do ElevenLabs, cai direto no Piper.
    """
    piper: Speaker | None = None
    try:
        piper = Speaker.from_config(config)  # None se a voz não estiver baixada
    except Exception as exc:
        print(f"[voz] Piper indisponível: {exc!r}", flush=True)

    engine = config.get("tts_engine")

    if engine == "kokoro":
        kokoro = KokoroSpeaker.from_config(config, fallback=piper)
        if kokoro is not None:
            print(f"[voz] Kokoro pronto: {kokoro.label}.", flush=True)
            return kokoro
        print("[voz] modelos do Kokoro ausentes — usando Piper.", flush=True)

    if engine == "xtts":
        try:
            from atlas.xtts_tts import XTTSSpeaker
            xtts = XTTSSpeaker.from_config(config, fallback=piper)
            if xtts is not None:
                print(f"[voz] XTTS pronto: {xtts.label}.", flush=True)
                return xtts
        except Exception as exc:
            print(f"[voz] XTTS indisponível ({exc!r}) — usando Piper.", flush=True)

    if engine == "elevenlabs":
        eleven = ElevenSpeaker.from_config(config, fallback=piper)
        if eleven is not None:
            return eleven
        print("[voz] ELEVENLABS_API_KEY ausente — usando Piper.", flush=True)

    return piper


# Apelidos amigáveis de MOTOR (o que a IA/usuário fala) -> chave interna do config.
_ENGINE_ALIASES = {
    "kokoro": "kokoro",
    "xtts": "xtts", "coqui": "xtts",
    "elevenlabs": "elevenlabs", "eleven": "elevenlabs", "premium": "elevenlabs",
    "piper": "piper", "local": "piper",
}

# Apelidos de VOZ -> (motor, chave_do_config, valor). Falar só o nome da voz já
# leva o motor junto (ex.: "usa a Dora" => kokoro/pf_dora). Aliases em minúsculo.
_VOICE_ALIASES = {
    # Kokoro (local, natural, padrão)
    "alex": ("kokoro", "kokoro_voice", "pm_alex"),
    "pm_alex": ("kokoro", "kokoro_voice", "pm_alex"),
    "santa": ("kokoro", "kokoro_voice", "pm_santa"),
    "pm_santa": ("kokoro", "kokoro_voice", "pm_santa"),
    "dora": ("kokoro", "kokoro_voice", "pf_dora"),
    "pf_dora": ("kokoro", "kokoro_voice", "pf_dora"),
    # Piper (local, fallback)
    "faber": ("piper", "tts_voice", "pt_BR-faber-medium"),
    "cadu": ("piper", "tts_voice", "pt_BR-cadu-medium"),
    "jeff": ("piper", "tts_voice", "pt_BR-jeff-medium"),
    "edresson": ("piper", "tts_voice", "pt_BR-edresson-low"),
    # XTTS (natural, porém lento)
    "royston": ("xtts", "xtts_speaker", "Royston Min"),
    "dionisio": ("xtts", "xtts_speaker", "Dionisio Schuyler"),
    "viktor": ("xtts", "xtts_speaker", "Viktor Eka"),
}

# Chave do config que guarda a voz de cada motor, e o conjunto de vozes VÁLIDAS
# (nomes canônicos). Recusamos qualquer voz fora daqui para nunca persistir um
# config quebrado que deixaria o TTS caindo no fallback a cada fala.
_ENGINE_VOICE_KEY = {
    "kokoro": "kokoro_voice",
    "xtts": "xtts_speaker",
    "piper": "tts_voice",
    "elevenlabs": "elevenlabs_voice_name",
}
_ENGINE_VOICES = {
    "kokoro": {"pm_alex", "pm_santa", "pf_dora"},
    "xtts": {"Royston Min", "Dionisio Schuyler", "Viktor Eka"},
    "piper": {"pt_BR-faber-medium", "pt_BR-cadu-medium",
              "pt_BR-jeff-medium", "pt_BR-edresson-low"},
    "elevenlabs": set(),  # voz específica não é validada/trocada por aqui
}


class VoiceController:
    """
    Troca o motor/voz do TTS em runtime, persiste no config.json e reconstrói o
    `SPEAKER` — o análogo, para a voz, do `set_default_model` do Claude Code. A
    escolha é "grudenta": vale daí em diante até o usuário mudar de novo.
    """

    def __init__(self, config: dict) -> None:
        self._config = config

    @property
    def current_label(self) -> str:
        return SPEAKER.label if SPEAKER is not None else "voz indisponível"

    async def switch(self, motor: str | None = None, voz: str | None = None) -> dict:
        motor = (motor or "").strip().lower()
        voz = (voz or "").strip()

        engine = None
        if motor:
            engine = _ENGINE_ALIASES.get(motor)
            if engine is None:
                return {"ok": False, "erro": f"motor desconhecido: {motor!r}"}

        voice_key = voice_val = None
        if voz:
            alias = _VOICE_ALIASES.get(voz.lower())
            if alias is not None:
                # A voz conhecida já carrega o motor certo (tem precedência).
                engine, voice_key, voice_val = alias
            else:
                # Nome canônico? Aplica ao motor pedido (ou ao atual). Senão, recusa
                # — não persistimos uma voz inválida que quebraria a síntese.
                eng = engine or self._config.get("tts_engine")
                if voz in _ENGINE_VOICES.get(eng, set()):
                    voice_key, voice_val = _ENGINE_VOICE_KEY[eng], voz
                else:
                    return {"ok": False,
                            "erro": f"voz desconhecida para o motor {eng!r}: {voz!r}"}

        if engine is None and voice_key is None:
            return {"ok": False, "erro": "nada a trocar: informe motor e/ou voz"}

        if engine is not None:
            self._config["tts_engine"] = engine
        if voice_key is not None and voice_val is not None:
            self._config[voice_key] = voice_val
        save_config(self._config)

        # Reconstrói a voz (pode carregar modelo/subir worker) fora do event loop.
        global SPEAKER
        try:
            SPEAKER = await asyncio.to_thread(_make_speaker, self._config)
        except Exception as exc:
            return {"ok": False, "erro": f"falha ao trocar a voz: {exc!r}"}

        if SPEAKER is None:
            return {"ok": False, "erro": "nenhuma voz disponível após a troca"}
        print(f"[voz] motor trocado em runtime: {SPEAKER.label}.", flush=True)
        await _broadcast(
            _encode("voice_ready", {"stt": TRANSCRIBER is not None, "tts": True})
        )
        return {"ok": True, "motor": self._config.get("tts_engine"),
                "voz_atual": SPEAKER.label}


async def main() -> None:
    parser = argparse.ArgumentParser(description="ATLAS I backend")
    parser.add_argument("--port", type=int, default=8765, help="porta do WebSocket")
    args = parser.parse_args()

    config = load_config()
    print(f"[boot] config carregada: usuario={config.get('usuario_nome')}", flush=True)

    global BRAIN
    BRAIN = _make_brain(config)
    if BRAIN is None:
        provider = config.get("ia_provider", "openai")
        chave = "OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY"
        print(f"[boot] {chave} ausente — modo eco (sem cérebro).", flush=True)
    else:
        print(
            f"[boot] cérebro ativo: {type(BRAIN).__name__} ({BRAIN.model})",
            flush=True,
        )

    # Carrega os modelos de voz em segundo plano (não atrasa a UI).
    asyncio.create_task(_load_voice(config))

    stop = asyncio.Event()

    # Proatividade: ATLAS puxa a conversa de tempos em tempos.
    if BRAIN is not None:
        asyncio.create_task(proactive_loop(config, stop))

    def _shutdown() -> None:
        print("[boot] sinal de encerramento recebido", flush=True)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:  # pragma: no cover
            pass

    # max_size alto: áudio (mic e TTS) trafega em base64 pelo mesmo canal.
    async with websockets.serve(
        handle_client, HOST, args.port, max_size=16 * 1024 * 1024
    ):
        # A UI (main.js) aguarda exatamente esta linha no stdout.
        print(f"ATLAS_BACKEND_READY {HOST}:{args.port}", flush=True)
        await stop.wait()

    print("[boot] backend encerrado.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

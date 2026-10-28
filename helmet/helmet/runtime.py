"""
Estado vivo de cada capacete — a máquina de estados da escuta.

            "Jarvis" (ou botão)                    pedido
    IDLE ───────────────────────► LISTENING ─────────────► THINKING ──► SPEAKING
     ▲                                 ▲                                    │
     │   120 s de AFK real / "pode     └────────────────────────────────────┘
     └────────── desligar" ◄────────── (qualquer fala renova os 120 s)

- **IDLE**: tudo que é dito é transcrito, mas só "Jarvis" faz algo. Nada é gravado.
- **LISTENING**: qualquer fala vira pedido ao cérebro e renova o prazo.
- O prazo só corre quando NADA está acontecendo: nem o usuário falando, nem
  enunciado na fila, nem o JARVIS pensando/falando. Ou seja, 2 min de AFK real.

O runtime sobrevive a quedas de Wi-Fi: o estado é por `device_id`, não por
conexão. Se o capacete reconectar dentro do prazo, a escuta continua.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

from helmet.audio.playback import PcPlayer
from helmet.audio.stt import is_hallucination, normalize
from helmet.audio.tts import split_sentences
from helmet.audio.vad import Segmenter, SpeechEnd, SpeechStart
from helmet.brain import persona_addendum
from helmet.i18n import t
from helmet.conversations import ConversationService, Metrics
from helmet.display import DisplaySpec, fit, max_chars
from helmet.events import EventBus
from helmet.settings import Settings
from helmet.tones import tone_message
from helmet.wake import find_wake, is_sleep_command, strip_leading_wake

log = logging.getLogger("helmet.runtime")



class DeviceLink(Protocol):
    async def send_json(self, data: dict) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


@dataclass
class Capabilities:
    mic_rate: int = 16_000
    speaker: bool = False
    speaker_rate: int = 16_000
    firmware: str | None = None
    display: DisplaySpec | None = None  # LCD/OLED declarado no hello (None = sem tela)


@dataclass
class Models:
    """Modelos pesados, carregados em segundo plano no boot."""

    stt: object | None = None  # SpeechRecognizer (pedidos, escuta ativa)
    stt_wake: object | None = None  # SpeechRecognizer (dormindo: achar "Jarvis")
    tts: object | None = None  # VoiceSynth
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.stt is not None and self.stt_wake is not None


@dataclass
class Utterance:
    t_end: float
    pcm: bytes | None = None
    audio_ms: int | None = None
    text: str | None = None
    source: str = "voice"
    force_active: bool = False
    future: asyncio.Future | None = field(default=None, repr=False)


class Hub:
    """Registro de capacetes + serviços compartilhados."""

    def __init__(
        self,
        settings: Settings,
        service: ConversationService,
        events: EventBus,
        models: Models,
        brain_factory: Callable[[], object],
        vad_classifier: Callable[[bytes, int], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.vad_classifier = vad_classifier
        self.service = service
        self.events = events
        self.models = models
        self.brain_factory = brain_factory
        self.devices: dict[str, DeviceRuntime] = {}

    def get(self, device_id: str) -> "DeviceRuntime":
        rt = self.devices.get(device_id)
        if rt is None:
            rt = self.devices[device_id] = DeviceRuntime(device_id, self)
        return rt

    async def shutdown(self) -> None:
        for rt in list(self.devices.values()):
            await rt.close("shutdown")


class DeviceRuntime:
    def __init__(self, device_id: str, hub: Hub) -> None:
        self.id = device_id
        self.hub = hub
        self.s = hub.settings
        self.caps = Capabilities(
            mic_rate=self.s.input_sample_rate, speaker_rate=self.s.output_sample_rate
        )
        self.link: DeviceLink | None = None
        self.ip: str | None = None
        self.state = "idle"
        self.active = False
        self.session_id: int | None = None
        self.conversation_id: int | None = None
        self.last_activity = time.monotonic()
        self._ctx_conversation: int | None = None
        self._brain = None
        self._queue: asyncio.Queue[Utterance] = asyncio.Queue()
        self._pending = 0
        self._processing = False
        self._speaking = False
        self._lock = asyncio.Lock()
        self._brain_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._audio_seq = 0
        self._playback_done = asyncio.Event()
        self._played_seq = 0
        self._last_warmup = 0.0
        self._rx_bytes = 0
        self._rx_t0 = time.monotonic()
        self._speech_t0 = 0.0
        self.segmenter = self._make_segmenter()
        self._hpf = self._make_hpf()
        self._tasks = [
            asyncio.create_task(self._worker(), name=f"worker:{device_id}"),
            asyncio.create_task(self._watchdog(), name=f"watchdog:{device_id}"),
        ]

    # ---- conexão ----------------------------------------------------------

    def _make_segmenter(self) -> Segmenter:
        s = self.s
        return Segmenter(
            sample_rate=self.caps.mic_rate,
            frame_ms=s.vad_frame_ms,
            classifier=self.hub.vad_classifier,
            start_ms=s.vad_start_ms,
            end_silence_ms=s.vad_end_silence_ms,
            preroll_ms=s.vad_preroll_ms,
            max_utterance_s=s.max_utterance_s,
            aggressiveness=s.vad_aggressiveness,
            min_rms=s.vad_min_rms,
            noise_factor=s.vad_noise_factor,
        )

    def _t(self, key: str) -> str:
        return t(self.s.language, key)

    def _make_hpf(self):  # noqa: ANN202
        if self.s.input_highpass_hz <= 0:
            return None
        from helmet.audio.filters import HighPass

        return HighPass(self.s.input_highpass_hz, self.caps.mic_rate)

    async def attach(self, link: DeviceLink, ip: str | None = None) -> None:
        old, self.link, self.ip = self.link, link, ip
        if old is not None and old is not link:
            try:
                await old.close(code=4000)  # conexão substituída
            except Exception:
                pass
        self.segmenter.reset()
        await self.hub.service.touch_device(self.id, ip=ip)
        self._publish({"type": "device_connected", "ip": ip})
        await self._send(self._welcome())
        await self._send(tone_message("ready"))
        await self._send_state()

    def detach(self, link: DeviceLink) -> None:
        if self.link is link:
            self.link = None
            self.segmenter.reset()
            self._playback_done.set()  # não fica esperando um "playback_done"
            self._publish({"type": "device_disconnected"})

    async def configure(self, hello: dict) -> None:
        """Mensagem `hello` do firmware: capacidades do hardware."""
        c = self.caps
        c.mic_rate = int(hello.get("mic_rate", c.mic_rate))
        c.speaker = bool(hello.get("speaker", c.speaker))
        c.speaker_rate = int(hello.get("speaker_rate", c.speaker_rate))
        c.firmware = hello.get("fw") or c.firmware
        try:  # sem o campo = sem display (comportamento original)
            c.display = DisplaySpec.parse(hello.get("display"))
        except (TypeError, ValueError) as exc:
            c.display = None
            await self._send({"type": "error", "error": f"display inválido: {exc}"})
        if c.mic_rate not in (8_000, 16_000, 32_000, 48_000):
            await self._send({"type": "error", "error": "mic_rate deve ser 8000/16000/32000/48000"})
            c.mic_rate = 16_000
        self.segmenter = self._make_segmenter()
        self._hpf = self._make_hpf()
        await self.hub.service.touch_device(self.id, firmware=c.firmware)
        await self._send(self._welcome())
        await self._send_state()

    def _welcome(self) -> dict:
        return {
            "type": "welcome",
            "device_id": self.id,
            "server": "jarvis-capacete",
            "caps": asdict(self.caps),
            "audio_target": self._audio_target(),
            "active_timeout_s": self.s.active_timeout_s,
            "models_ready": self.hub.models.ready,
            "display": self._display_info(),
        }

    def _display_info(self) -> dict | None:
        d = self.caps.display
        if d is None:
            return None
        return {
            "cols": d.cols, "rows": d.rows, "ascii": d.ascii,
            "max_chars": max_chars(d, self.s.display_max_chars),
            "ttl_ms": self.s.display_ttl_ms,
            # "display" = respostas curtas p/ a tela; "voice" = tem alto-falante,
            # então a resposta segue o estilo falado e a tela mostra um resumo cortado.
            "reply_style": "display" if not self.caps.speaker else "voice",
        }

    # ---- entrada ----------------------------------------------------------

    def feed_audio(self, pcm: bytes) -> None:
        """PCM s16le mono do microfone (na taxa `caps.mic_rate`)."""
        if self.s.debug_audio_dir:
            self._debug_rate(len(pcm), pcm)
        if self._speaking and self.s.mute_mic_while_speaking:
            return
        if not self.hub.models.ready:
            return
        if self._hpf is not None:
            pcm = self._hpf.process(pcm)
        for ev in self.segmenter.feed(pcm):
            if isinstance(ev, SpeechStart):
                # Não renova o prazo aqui (pode ser ruído): enquanto há fala em
                # curso o prazo fica PAUSADO (_busy) e só texto real o renova.
                self._publish({"type": "speech_start"})
                self._speech_t0 = time.monotonic()
                self._warm_brain()
            elif isinstance(ev, SpeechEnd):
                self._publish({"type": "speech_end", "duration_ms": ev.duration_ms})
                if self.s.debug_audio_dir:
                    self._debug_utterance(ev)
                if ev.duration_ms < self.s.min_utterance_ms:
                    continue
                self._enqueue(Utterance(t_end=time.monotonic(), pcm=ev.pcm, audio_ms=ev.duration_ms))

    def _debug_rate(self, n: int, pcm: bytes = b"") -> None:
        from pathlib import Path

        raw = Path(self.s.debug_audio_dir).expanduser() / f"bruto-{self.id}.pcm"
        raw.parent.mkdir(parents=True, exist_ok=True)
        if not raw.exists() or raw.stat().st_size < 20 * 1024 * 1024:  # ~10 min
            with open(raw, "ab") as fh:
                fh.write(pcm)
        self._rx_bytes += n
        dt = time.monotonic() - self._rx_t0
        if dt >= 5:
            rate = self._rx_bytes / dt
            expected = self.caps.mic_rate * 2
            log.info("[%s] mic: %.1f KB/s recebidos (esperado %.1f KB/s = %.0f%%)",
                     self.id, rate / 1024, expected / 1024, 100 * rate / expected)
            self._rx_bytes, self._rx_t0 = 0, time.monotonic()

    def _debug_utterance(self, ev: SpeechEnd) -> None:
        import numpy as np
        from pathlib import Path

        from helmet.audio.resample import pcm_to_wav

        a = np.frombuffer(ev.pcm, dtype="<i2").astype(np.float32)
        wall = int((time.monotonic() - self._speech_t0) * 1000) if self._speech_t0 else -1
        d = Path(self.s.debug_audio_dir).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{time.strftime('%H%M%S')}-{self.id}-{ev.duration_ms}ms.wav"
        f.write_bytes(pcm_to_wav(ev.pcm, self.caps.mic_rate))
        log.info("[%s] enunciado: áudio=%dms relógio=%dms forçado=%s rms=%.0f pico=%.0f -> %s",
                 self.id, ev.duration_ms, wall, ev.forced,
                 float(np.sqrt((a ** 2).mean())) if a.size else 0,
                 float(np.abs(a).max()) if a.size else 0, f.name)

    async def submit_text(self, text: str, force_active: bool = False) -> dict:
        """Texto como se tivesse sido falado (testes / outros clientes)."""
        fut = asyncio.get_running_loop().create_future()
        self._enqueue(
            Utterance(t_end=time.monotonic(), text=text, source="text",
                      force_active=force_active, future=fut)
        )
        return await fut

    def _warm_brain(self) -> None:
        """Abre a conexão com a OpenAI ENQUANTO o usuário ainda fala."""
        now = time.monotonic()
        if now - self._last_warmup < 30:
            return
        self._last_warmup = now

        async def _go() -> None:
            brain = await self._get_brain()
            warm = getattr(brain, "warmup", None)
            if warm is not None:
                await warm()

        asyncio.create_task(_go())

    async def _get_brain(self):  # noqa: ANN202
        if self._brain is None:
            async with self._brain_lock:
                if self._brain is None:
                    self._brain = await asyncio.to_thread(self.hub.brain_factory)
        return self._brain

    def _enqueue(self, u: Utterance) -> None:
        self._pending += 1
        self._queue.put_nowait(u)

    def playback_done(self, seq: int) -> None:
        self._played_seq = max(self._played_seq, seq)
        self._playback_done.set()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    # ---- ativação ---------------------------------------------------------

    async def activate(self, wake_text: str | None, reason: str = "wake") -> bool:
        async with self._lock:
            if self.active:
                return False
            opened = await self.hub.service.open_session(self.id, wake_text)
            self.session_id = opened.session_id
            self.conversation_id = opened.conversation_id
            self.active = True
            self.touch()
            self.state = "listening"
        self._publish({
            "type": "activated", "reason": reason, "session_id": opened.session_id,
            "conversation_id": opened.conversation_id,
            "new_conversation": opened.new_conversation,
        })
        await self._send(tone_message("wake"))
        await self._send_status_frame(self._t("listening"), ttl_ms=0)
        await self._send_state()
        return True

    async def deactivate(self, reason: str) -> bool:
        async with self._lock:
            if not self.active:
                return False
            self.active = False
            sid, self.session_id = self.session_id, None
            self.state = "idle"
            closed = await self.hub.service.close_session(sid, reason)
            if closed is not None:
                self.conversation_id = None
        self._publish({"type": "deactivated", "reason": reason, "session_id": sid,
                       "closed_conversation_id": closed})
        await self._send(tone_message("sleep"))
        await self._send_status_frame(self._t("standby"), ttl_ms=self.s.display_sleep_ttl_ms)
        await self._send_state()
        return True

    def expires_in(self) -> float | None:
        if not self.active:
            return None
        if self._busy():
            return self.s.active_timeout_s
        return max(0.0, self.s.active_timeout_s - (time.monotonic() - self.last_activity))

    def _busy(self) -> bool:
        return self._processing or self._pending > 0 or self.segmenter.in_speech

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            try:
                if (
                    self.active
                    and not self._busy()
                    and time.monotonic() - self.last_activity >= self.s.active_timeout_s
                ):
                    await self.deactivate("timeout")
            except Exception:
                log.exception("[%s] watchdog", self.id)

    # ---- processamento ----------------------------------------------------

    async def _worker(self) -> None:
        while True:
            u = await self._queue.get()
            self._processing = True
            try:
                result = await self._handle(u)
                if u.future and not u.future.done():
                    u.future.set_result(result)
            except Exception as exc:
                log.exception("[%s] falha ao tratar enunciado", self.id)
                if u.future and not u.future.done():
                    u.future.set_exception(exc)
            finally:
                self._pending -= 1
                self._processing = False

    async def _handle(self, u: Utterance) -> dict:
        stt_ms = None
        if u.pcm is not None:
            models = self.hub.models
            stt = models.stt if self.active else models.stt_wake
            t0 = time.monotonic()
            text = await asyncio.to_thread(stt.transcribe_pcm, u.pcm, self.caps.mic_rate)
            stt_ms = _ms(t0)
            log.info("[%s] ouviu (%s, %sms): %r", self.id,
                     "ativo" if self.active else "dormindo", stt_ms, text)
            self._publish({"type": "transcript", "text": text, "stt_ms": stt_ms,
                           "audio_ms": u.audio_ms, "active": self.active})
        else:
            text = u.text or ""
        text = text.strip()
        if not text:
            return {"status": "empty"}

        if not self.active:
            if u.force_active:
                await self.activate(text, reason="manual")
                command = strip_leading_wake(text)
            else:
                m = find_wake(text)
                if m is None:
                    log.debug("[%s] ignorado (dormindo): %s", self.id, text)
                    return {"status": "ignored", "text": text}
                await self.activate(text)
                command = m.command
            if not _meaningful(command):
                return {"status": "activated", "text": text}
        else:
            self.touch()
            command = strip_leading_wake(text)
            if not _meaningful(command):
                await self._send(tone_message("ack"))
                return {"status": "noop", "text": text}

        if is_sleep_command(command):
            await self.deactivate("command")
            return {"status": "sleep", "text": text}
        return await self._converse(command, u, stt_ms)

    async def _converse(self, text: str, u: Utterance, stt_ms: int | None) -> dict:
        sid = self.session_id
        metrics = Metrics(audio_ms=u.audio_ms, stt_ms=stt_ms)
        await self._send({"type": "transcript", "text": text})
        await self._set_state("thinking")
        await self._send(tone_message("ack"))

        brain = await self._brain_for_conversation()
        t0 = time.monotonic()
        try:
            reply = (await brain.reply(text, on_action=self._on_action)).strip()
            if not reply:
                reply = self._t("empty_reply")
        except Exception as exc:
            log.exception("[%s] cérebro falhou", self.id)
            reply, metrics.error = self._t("error_reply"), repr(exc)[:500]
            await self._send(tone_message("error"))
        metrics.brain_ms = _ms(t0)
        self._publish({"type": "reply", "text": reply, "brain_ms": metrics.brain_ms})
        await self._send({"type": "reply", "text": reply})
        if self.caps.display is not None:
            await self._send_display(self._t("display_error") if metrics.error else reply, "reply",
                                     self.s.display_ttl_ms)

        first_audio = None
        try:
            metrics.tts_first_ms, first_audio = await self._speak(reply)
        except Exception as exc:
            log.exception("[%s] fala falhou", self.id)
            metrics.error = metrics.error or f"tts: {exc!r}"[:500]
        finally:
            metrics.total_ms = int(((first_audio or time.monotonic()) - u.t_end) * 1000)
            if sid is not None:
                await self.hub.service.record_interaction(
                    sid, text, reply, source=u.source, metrics=metrics
                )
            await self._set_state("listening" if self.active else "idle")
            self.touch()
        log.info(
            "[%s] stt=%sms cérebro=%sms tts1=%sms total=%sms | %s → %s",
            self.id, metrics.stt_ms, metrics.brain_ms, metrics.tts_first_ms,
            metrics.total_ms, text, reply,
        )
        return {"status": "ok", "text": text, "reply": reply, "metrics": asdict(metrics)}

    async def _on_action(self, info: dict) -> None:
        await self._send({"type": "status", "text": f"executando no Claude Code ({info.get('projeto') or 'base'})"})

    async def _brain_for_conversation(self):  # noqa: ANN202
        await self._get_brain()
        set_channel = getattr(self._brain, "set_channel", None)
        if set_channel is not None:
            set_channel(persona_addendum(self.caps.display, self.caps.speaker,
                                         self.s.display_max_chars, lang=self.s.language))
        if self.conversation_id is not None and self._ctx_conversation != self.conversation_id:
            pairs = await self.hub.service.recent_context(
                self.conversation_id, self.s.context_interactions
            )
            self._brain.load_context(pairs)
            self._ctx_conversation = self.conversation_id
        return self._brain

    # ---- fala -------------------------------------------------------------

    def _audio_target(self) -> str:
        mode = self.s.reply_audio
        if mode == "auto":
            if self.caps.speaker and self.link is not None:
                return "device"
            return "pc" if PcPlayer.available() else "none"
        if mode == "device" and self.link is None:
            return "none"
        return mode

    async def _speak(self, text: str) -> tuple[int | None, float | None]:
        """Sintetiza frase a frase e toca no destino. -> (tts_first_ms, t_1º_áudio)."""
        tts = self.hub.models.tts
        target = self._audio_target()
        if target == "none" or tts is None:
            return None, None
        sentences = split_sentences(text)
        if not sentences:
            return None, None
        rate = self.caps.speaker_rate if target == "device" else self.s.output_sample_rate

        q: asyncio.Queue[bytes | None] = asyncio.Queue()  # sem teto: poucas frases
        t0 = time.monotonic()
        first_synth: list[int] = []

        async def produce() -> None:
            try:
                for s in sentences:
                    pcm = await asyncio.to_thread(tts.synth_pcm, s, rate)
                    if not first_synth:
                        first_synth.append(_ms(t0))
                    if pcm:
                        q.put_nowait(pcm)
            finally:
                q.put_nowait(None)

        self._speaking = True
        self.segmenter.reset()
        await self._set_state("speaking")
        producer = asyncio.create_task(produce())
        try:
            if target == "device":
                first = await self._stream_to_device(q, rate)
            else:
                first = await self._play_on_pc(q, rate)
            await producer
        finally:
            producer.cancel()
            self._speaking = False
            self.segmenter.reset()
        return (first_synth[0] if first_synth else None), first

    async def _play_on_pc(self, q: asyncio.Queue, rate: int) -> float | None:
        player = PcPlayer(rate)
        first = None
        await player.start()
        try:
            while (pcm := await q.get()) is not None:
                if first is None:
                    first = time.monotonic()
                await player.write(pcm)
        finally:
            await player.finish()
        return first

    async def _stream_to_device(self, q: asyncio.Queue, rate: int) -> float | None:
        """
        Stream PCM em tempo real (no máximo `stream_lead_ms` à frente), para não
        estourar o buffer pequeno do ESP32. Depois espera tocar até o fim.
        """
        self._audio_seq += 1
        seq = self._audio_seq
        self._playback_done.clear()
        chunk = rate * 2 * self.s.stream_chunk_ms // 1000
        lead = self.s.stream_lead_ms / 1000
        first = None
        play_end = 0.0  # instante (monotonic) em que o capacete termina o que já recebeu
        total_ms = 0
        await self._send({"type": "audio_start", "seq": seq, "rate": rate,
                          "format": "s16le", "channels": 1})
        while (pcm := await q.get()) is not None:
            for i in range(0, len(pcm), chunk):
                piece = pcm[i:i + chunk]
                now = time.monotonic()
                if first is None:
                    first = now
                ahead = play_end - now
                if ahead > lead:
                    await asyncio.sleep(ahead - lead)
                if self.link is None:
                    raise ConnectionError("capacete desconectou durante a fala")
                await self._send_bytes(piece)
                dur = len(piece) / 2 / rate
                play_end = max(play_end, time.monotonic()) + dur
                total_ms += int(dur * 1000)
        await self._send({"type": "audio_end", "seq": seq, "duration_ms": total_ms})
        remaining = play_end - time.monotonic()
        if remaining > 0:
            try:  # o firmware pode avisar antes com {"type":"playback_done"}
                await asyncio.wait_for(self._wait_played(seq), timeout=remaining + 0.3)
            except asyncio.TimeoutError:
                pass
        return first

    async def _wait_played(self, seq: int) -> None:
        while self._played_seq < seq and self.link is not None:
            self._playback_done.clear()
            await self._playback_done.wait()

    # ---- saída p/ o capacete ---------------------------------------------

    async def _send_display(self, text: str, kind: str, ttl_ms: int) -> None:
        d = self.caps.display
        if d is None:
            return
        frame = fit(text, d, max_chars(d, self.s.display_max_chars))
        msg = {"type": "display", "text": frame.text, "lines": frame.lines,
               "ttl_ms": ttl_ms, "kind": kind}
        self._publish(msg)
        await self._send(msg)

    async def _send_status_frame(self, text: str, ttl_ms: int) -> None:
        if self.s.display_status:
            await self._send_display(text, "status", ttl_ms)

    async def _set_state(self, state: str) -> None:
        self.state = state
        await self._send_state()

    async def _send_state(self) -> None:
        exp = self.expires_in()
        await self._send({
            "type": "state", "state": self.state, "active": self.active,
            "expires_in": round(exp, 1) if exp is not None else None,
        })
        self._publish({"type": "state", "state": self.state, "active": self.active})

    async def _send(self, data: dict) -> None:
        link = self.link
        if link is None:
            return
        try:
            async with self._send_lock:
                await link.send_json(data)
        except Exception as exc:
            log.info("[%s] envio falhou (%r) — desconectado?", self.id, exc)

    async def _send_bytes(self, data: bytes) -> None:
        link = self.link
        if link is None:
            return
        async with self._send_lock:
            await link.send_bytes(data)

    def _publish(self, event: dict) -> None:
        self.hub.events.publish({"device_id": self.id, **event})

    # ---- API / desligamento -----------------------------------------------

    def snapshot(self) -> dict:
        exp = self.expires_in()
        return {
            "device_id": self.id,
            "connected": self.link is not None,
            "ip": self.ip,
            "state": self.state,
            "active": self.active,
            "expires_in": round(exp, 1) if exp is not None else None,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "queue": self._pending,
            "audio_target": self._audio_target(),
            "caps": asdict(self.caps),
        }

    async def close(self, reason: str) -> None:
        for t in self._tasks:
            t.cancel()
        if self.active:
            await self.deactivate(reason)


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _meaningful(command: str) -> bool:
    n = normalize(command)
    return len(n) >= 2 and not is_hallucination(command)

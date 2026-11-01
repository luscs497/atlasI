"""Montagem da aplicação FastAPI (injeção de dependências p/ testes)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI

from helmet import __version__
from helmet.api.routes import router
from helmet.conversations import ConversationService, Summarizer
from helmet.db import init_db, make_engine, make_sessionmaker
from helmet.events import EventBus
from helmet.runtime import Hub, Models
from helmet.settings import Settings, get_settings

log = logging.getLogger("helmet")


def _load_models(settings: Settings, models: Models) -> None:
    """Carrega STT + TTS (lento: segundos). Roda numa thread no boot."""
    from helmet.audio.stt import SpeechRecognizer
    from helmet.audio.tts import VoiceSynth

    try:
        from helmet.i18n import t

        voice, voice_lang = settings.voice()
        tts = VoiceSynth.from_atlas_config(voice=voice, lang=voice_lang)
        if tts is not None:
            tts.synth_pcm(t(settings.language, "warmup"), settings.output_sample_rate)  # aquece
        models.tts = tts
        log.info("voz pronta: %s", tts.label if tts else "INDISPONÍVEL")
        def _stt(size: str) -> SpeechRecognizer:
            r = SpeechRecognizer(
                model_size=size,
                language=settings.stt_lang,
                initial_prompt=t(settings.language, "stt_prompt"),
                beam_size=settings.stt_beam_size,
                cpu_threads=settings.stt_cpu_threads,
            )
            r.warmup()
            return r

        wake = _stt(settings.stt_wake_model)
        main = wake if settings.stt_model == settings.stt_wake_model else _stt(settings.stt_model)
        models.stt, models.stt_wake = main, wake
        log.info("ouvido pronto: whisper %s (dormindo) / %s (pedidos), beam %d",
                 settings.stt_wake_model, settings.stt_model, settings.stt_beam_size)
    except Exception as exc:
        models.error = repr(exc)
        log.exception("falha ao carregar modelos de voz")


def create_app(
    settings: Settings | None = None,
    *,
    models: Models | None = None,
    brain_factory: Callable[[], object] | None = None,
    summarizer: Summarizer | None = None,
    use_default_summarizer: bool = True,
    vad_classifier: Callable[[bytes, int], bool] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    preload = models is None
    models = models or Models()

    if brain_factory is None:
        from helmet.brain import build_brain

        def brain_factory() -> object:
            return build_brain(settings.allow_code, settings.brain_model)

    if summarizer is None and use_default_summarizer:
        from helmet.summarizer import OpenAISummarizer

        summarizer = OpenAISummarizer.from_env(settings.summary_model)
        if summarizer is None:
            log.warning("sem OPENAI_API_KEY: conversas encerradas ficam sem título")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        await init_db(engine)
        sm = make_sessionmaker(engine)
        events = EventBus()
        service = ConversationService(
            sm, settings.conversation_max_bytes, summarizer,
            max_summary_attempts=settings.summary_max_attempts,
            on_event=events.publish,
        )
        hub = Hub(settings, service, events, models, brain_factory, vad_classifier)
        app.state.hub, app.state.sessionmaker = hub, sm
        await service.recover()

        loader = None
        if preload and settings.preload_models:
            loader = asyncio.create_task(asyncio.to_thread(_load_models, settings, models))

        mdns = None
        if settings.mdns_enabled:
            from helmet.discovery import start_mdns

            mdns = await start_mdns(settings)

        if not settings.token:
            log.warning("HELMET_TOKEN vazio: API e WebSocket SEM autenticação")
        log.info("capacete pronto em %s:%s", settings.host, settings.port)
        try:
            yield
        finally:
            await hub.shutdown()
            await service.drain(timeout=15)
            if mdns is not None:
                await mdns.close()
            if loader is not None and not loader.done():
                loader.cancel()
            await engine.dispose()

    app = FastAPI(title="ATLAS — capacete", version=__version__, lifespan=lifespan)
    app.include_router(router)
    return app

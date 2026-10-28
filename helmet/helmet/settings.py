"""
Configuração do backend do capacete.

Tudo vem de variáveis de ambiente com prefixo `HELMET_` (ou do `.env` na raiz do
projeto). Os segredos do cérebro (OPENAI_API_KEY etc.) são os MESMOS do JARVIS
desktop e também são lidos do `.env` da raiz.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # jarvis-v2/
BACKEND_DIR = PROJECT_ROOT / "backend"  # onde mora o pacote `jarvis`
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = Path.home() / ".jarvis"

# Chaves do cérebro (OPENAI_API_KEY, PERPLEXITY_API_KEY…) vão para o os.environ,
# que é onde o pacote `jarvis` as procura.
load_dotenv(ENV_FILE)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HELMET_", env_file=ENV_FILE, extra="ignore"
    )

    # ---- servidor ---------------------------------------------------------
    host: str = "0.0.0.0"  # aceita conexões da rede local (o ESP32)
    port: int = 8770
    # Token compartilhado com o firmware. Vazio = sem autenticação (só p/ dev).
    token: str = ""
    # Anuncia o serviço via mDNS (_jarvis-helmet._tcp) p/ o ESP32 achar o PC.
    mdns_enabled: bool = True
    mdns_name: str = "jarvis"

    # ---- banco ------------------------------------------------------------
    # SQLite local hoje; na VPS basta trocar por postgresql+asyncpg://…
    database_url: str = f"sqlite+aiosqlite:///{DATA_DIR / 'capacete.db'}"

    # ---- regras de escuta / conversa --------------------------------------
    # Tempo de AFK real (sem fala do usuário e sem o JARVIS ocupado) até o
    # capacete voltar a dormir e exigir "Jarvis" de novo.
    active_timeout_s: float = 120.0
    # Tamanho (bytes UTF-8 de texto) a partir do qual a conversa é encerrada —
    # mas só quando a escuta ativa terminar (nunca no meio de uma conversa ao vivo).
    conversation_max_bytes: int = 20_000
    # Quantas interações recentes da conversa recarregar no contexto do cérebro.
    context_interactions: int = 12

    # ---- áudio de entrada (microfone do capacete) --------------------------
    input_sample_rate: int = 16_000  # PCM s16le mono (padrão; o hello pode mudar)
    vad_aggressiveness: int = 2  # 0..3 (webrtcvad); 3 = mais rígido c/ ruído
    vad_frame_ms: int = 30  # 10, 20 ou 30
    vad_start_ms: int = 150  # fala contínua necessária para abrir um enunciado
    vad_end_silence_ms: int = 600  # silêncio que encerra o enunciado
    vad_preroll_ms: int = 300  # áudio guardado ANTES do início detectado
    vad_min_rms: float = 200.0  # piso de energia (int16) p/ ignorar chiado
    # Fala só conta se a energia passar de N x o ruído de fundo medido (0 = desliga).
    vad_noise_factor: float = 3.0
    # Passa-alta antes do VAD/Whisper (tira zumbido 60/120 Hz). 0 = desliga.
    input_highpass_hz: float = 200.0
    min_utterance_ms: int = 350  # enunciados menores que isso são descartados
    max_utterance_s: float = 30.0  # corta enunciados intermináveis
    # Descarta o microfone enquanto o JARVIS fala (evita ele ouvir a si mesmo).
    mute_mic_while_speaking: bool = True
    # Diagnóstico do mic: se definido, salva cada enunciado em WAV nesta pasta e
    # loga a taxa real de áudio recebida (bytes/s) e o nível de cada enunciado.
    debug_audio_dir: str = ""

    # ---- idioma -----------------------------------------------------------
    # pt = português (voz do desktop); en = inglês britânico, estilo JARVIS do filme
    # (você fala inglês, ele responde em inglês com a voz `bm_daniel`).
    language: Literal["pt", "en"] = "pt"
    # Voz Kokoro do capacete. Vazio = padrão do idioma (pt: a do desktop; en: bm_daniel).
    tts_voice: str = ""
    tts_lang: str = ""  # ex.: en-gb, en-us, pt-br (vazio = padrão do idioma)

    # ---- STT --------------------------------------------------------------
    # O Whisper processa sempre uma janela de 30 s: até um "Jarvis." curto custa
    # ~1 s no `small` (CPU) contra ~0,4 s no `base`. MAS, medido com ruído, o
    # `base` ouviu "Jarvis" como "Já vi" (não acorda) e "Argentina" como "dar
    # gentilha"; o `small` acertou. Padrão = `small` (confiável); `base` é a opção
    # de velocidade p/ mic limpo. Modelos iguais = uma instância só (menos RAM).
    stt_wake_model: str = "small"
    stt_model: str = "small"
    stt_language: str = ""  # vazio = o de `language`
    stt_beam_size: int = 1  # 1 = guloso (mais rápido); 5 = mais preciso
    stt_cpu_threads: int = 8

    # ---- saída de voz -----------------------------------------------------
    # auto   -> no capacete se ele declarar alto-falante, senão no PC
    # device -> sempre stream PCM p/ o capacete
    # pc     -> toca nas caixas do PC
    # none   -> só texto (+ bipes nos buzzers)
    reply_audio: Literal["auto", "device", "pc", "none"] = "auto"
    output_sample_rate: int = 16_000
    # Quanto áudio pode ir à frente do tempo real no stream p/ o capacete.
    stream_lead_ms: int = 400
    stream_chunk_ms: int = 100

    # ---- display (LCD/OLED I2C no capacete, declarado no hello) ------------
    # Teto de caracteres da resposta no canal de display (o limite real é
    # min(cols*rows, este teto)). Respostas maiores são cortadas em fim de palavra.
    display_max_chars: int = 60
    # Quanto tempo a resposta fica na tela (o firmware apaga depois). 0 = até o próximo quadro.
    display_ttl_ms: int = 8000
    # Quadros de status ("Ouvindo..." ao ativar, "Em espera." ao dormir).
    display_status: bool = True
    display_sleep_ttl_ms: int = 3000

    # ---- cérebro ----------------------------------------------------------
    # Modelo da OpenAI no capacete. Vazio = o mesmo do desktop (openai_model).
    brain_model: str = ""
    # Pedidos de código (Claude Code SEM confirmação) por voz pelo capacete.
    # Desligado por padrão: o microfone fica aberto e qualquer um pode dizer "Jarvis".
    allow_code: bool = False
    summary_model: str = "gpt-4o-mini"
    summary_max_attempts: int = 3

    # ---- carga de modelos -------------------------------------------------
    preload_models: bool = True


    @property
    def stt_lang(self) -> str:
        return self.stt_language or self.language

    def voice(self) -> tuple[str, str]:
        from helmet.i18n import DEFAULT_VOICE

        v, lang = DEFAULT_VOICE.get(self.language, ("", ""))
        return self.tts_voice or v, self.tts_lang or lang


def get_settings() -> Settings:
    return Settings()

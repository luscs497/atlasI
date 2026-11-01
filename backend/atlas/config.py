"""
Configuração e persistência local do ATLAS.

Os dados do usuário ficam em ~/.atlas/ (fora do repositório), conforme o
escopo. Aqui já criamos a base de config; a memória (memoria.json) é usada
a partir da fase de persistência.
"""

from __future__ import annotations

import json
from pathlib import Path

ATLAS_DIR = Path.home() / ".atlas"
CONFIG_PATH = ATLAS_DIR / "config.json"
MEMORY_PATH = ATLAS_DIR / "memoria.json"

DEFAULT_CONFIG: dict = {
    "ultimo_modo": "texto",
    "tts_engine": "elevenlabs",
    "proativo_intervalo_min": 30,
    "proativo_intervalo_max": 60,
    # Cérebro atual: OpenAI (gpt-4o-mini — barato e bom). Gemini (gratuito) e
    # Claude Haiku 4.5 ficam como alternativas — basta trocar `ia_provider`.
    "ia_provider": "openai",
    "openai_model": "gpt-4o-mini",
    "gemini_model": "gemini-2.5-flash",
    "claude_model": "claude-haiku-4-5-20251001",
    # Voz (local/gratuita por enquanto): STT via faster-whisper, TTS via Piper.
    # `small` é bem mais preciso em PT-BR que `base`; `medium` é o próximo degrau.
    "stt_model": "small",
    "stt_language": "pt",
    # Voz do ATLAS. Motor atual = ElevenLabs (voz "George", britânica/grave).
    # Piper (`tts_voice`) fica como fallback local quando a cota grátis acaba.
    "tts_voice": "pt_BR-faber-medium",
    # Kokoro (kokoro-onnx) — TTS local/gratuito, mais natural que o Piper. Vozes
    # PT-BR: pm_alex/pm_santa (masc.), pf_dora (fem.). Modelos em ~/.atlas/models/kokoro.
    "kokoro_voice": "pm_alex",
    "kokoro_lang": "pt-br",
    "kokoro_speed": 1.0,
    # XTTS-v2 (Coqui) — voz PT-BR muito natural, porém LENTA (venv isolada .venv-xtts,
    # PyTorch). Vozes masc.: 'Royston Min', 'Dionisio Schuyler', 'Viktor Eka'.
    "xtts_speaker": "Royston Min",
    "xtts_language": "pt",
    "elevenlabs_voice_id": "JBFqnCBsd6RMkjVDRZzb",
    "elevenlabs_voice_name": "George",
    "elevenlabs_model": "eleven_multilingual_v2",
    "elevenlabs_voice_settings": {
        "stability": 0.85,  # voz firme/constante (escolha do usuário)
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
    },
    "usuario_nome": "Lucas",
    # Integração com o Claude Code (ações de programação despachadas pela IA).
    # Roda headless e SEM confirmação (--dangerously-skip-permissions), escopo
    # limitado a `claude_code_base_dir`. O modelo é "grudento": pode ser trocado
    # por voz/texto ("use o Opus 4.8…") e vira o novo padrão até mudar de novo.
    "claude_code_enabled": True,
    "claude_code_base_dir": "~/projetos",
    "claude_code_model": "claude-sonnet-4-6",
    "claude_code_timeout": 600,
    "claude_code_max_rounds": 3,
    # Sistema de tarefas (banco SQLite local em ~/.atlas/atlas.db). Listas fixas
    # Trabalho (por projeto) e Pessoal, manipuladas pela IA via tool-calling.
    "tasks_enabled": True,
    # Rastreamento de tempo por projeto (mesmo banco). "começando o X" inicia
    # cronômetro; relatório de tempo real por projeto/período.
    "time_tracking_enabled": True,
    # Finanças pessoais (mesmo banco). "gastei 50 no mercado" registra; resumo
    # de gastos por categoria/período (mês por padrão).
    "finance_enabled": True,
    # Hábitos / desenvolvimento pessoal (mesmo banco). Hábitos construir/quebrar,
    # check-in diário por voz, sequência e feedback honesto de consistência.
    "habits_enabled": True,
    # Segundo cérebro (mesmo banco): ideias conectadas por embedding ao longo do
    # tempo. Usa a API de embeddings da OpenAI (barata); cai p/ palavra-chave sem.
    "second_brain_enabled": True,
    "embedding_model": "text-embedding-3-small",
    # Tela de visualizações dinâmicas (2ª janela Electron). O cérebro monta specs
    # (gráficos/listas/kpis/mapas) e o backend faz broadcast p/ a janela desenhar.
    "viz_enabled": True,
    # Oráculo: quando o GPT não sabe/trava, encaminha a pergunta ao Claude (headless
    # `claude -p`) e usa a resposta. GPT barato na rotina, Claude p/ pensar mais.
    "oraculo_enabled": True,
    "oraculo_model": "claude-sonnet-4-6",
    "oraculo_timeout": 120,
    # Perplexity Sonar: dados em TEMPO REAL (cotações, clima, notícias). Chave em
    # PERPLEXITY_API_KEY (.env). `sonar-pro` é o modelo online premium atual — o
    # `llama-3.1-sonar-large-128k-online` foi descontinuado pela Perplexity.
    "perplexity_enabled": True,
    "perplexity_model": "sonar-pro",
    "perplexity_timeout": 45,
}

# Migrações: chave -> (valor_antigo_a_substituir, valor_novo). Aplicadas na carga
# para atualizar escolhas defasadas sem sobrescrever preferências do usuário.
_MIGRATIONS: dict = {
    "stt_model": ("base", "small"),  # sobe a qualidade do STT
    "ia_provider": ("gemini", "openai"),  # cérebro Gemini -> OpenAI (gpt-4o-mini)
}


def _ensure_dir() -> None:
    ATLAS_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Lê ~/.atlas/config.json, criando com defaults se não existir."""
    _ensure_dir()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}

    # Preenche chaves ausentes com defaults (migração suave).
    merged = {**DEFAULT_CONFIG, **data}

    # Aplica migrações de valores defasados (ex.: stt_model base -> small).
    for chave, (antigo, novo) in _MIGRATIONS.items():
        if merged.get(chave) == antigo:
            merged[chave] = novo

    if merged != data:
        save_config(merged)
    return merged


def save_config(config: dict) -> None:
    _ensure_dir()
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

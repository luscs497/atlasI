# JARVIS v2

Assistente pessoal estilo Iron Man para **Linux (Pop!_OS)**: interface holográfica em
**Electron** + cérebro em **Python**, conversando por **WebSocket local**. Fala, escuta,
lembra de você entre sessões, puxa assunto sozinho, gerencia tarefas, tempo, finanças e
hábitos, desenha gráficos e ainda tem um **capacete físico (ESP32)** que conversa com o
mesmo JARVIS.

```
 Electron (HUD) ──WebSocket 127.0.0.1──► backend/ (Python)  ──► OpenAI · Perplexity · Claude
                                              │
 Capacete ESP32 ──Wi-Fi/WebSocket──► helmet/ (FastAPI) ──► usa o mesmo pacote jarvis/
```

## Funcionalidades

| Área | O que faz |
|---|---|
| **Dois modos de UI** | Texto (terminal estilo Claude Code) e voz (orbe fullscreen áudio-reativo). `Ctrl+Shift` alterna. |
| **Cérebro** | OpenAI `gpt-4o-mini` com *tool calling* multi-turno (Gemini como alternativa, via `ia_provider`). |
| **Voz** | STT local com `faster-whisper`; TTS local com **Kokoro** (padrão), Piper (fallback), XTTS-v2 (mais natural e lento, venv isolada) ou ElevenLabs. |
| **Memória** | Episódica (últimas trocas recarregadas no boot) + semântica (fatos sobre você, extraídos automaticamente ou por "lembre-se que…"). |
| **Proatividade** | Inicia conversas em intervalos aleatórios, sem interromper e respeitando uma janela de silêncio. |
| **Produtividade** | Tarefas/projetos, controle de tempo, finanças (gastos e relatórios), hábitos e **segundo cérebro** (ideias com busca semântica). |
| **Visualizações** | Gráficos e designs em HTML + Chart.js/D3 gerados sob demanda, que podem ser salvos e reabertos. |
| **Tempo real** | Perplexity Sonar (`sonar-pro`) para cotações, clima, notícias. |
| **Oráculo Claude** | Quando o GPT não sabe, consulta o Claude via CLI `claude`; também consegue executar tarefas no **Claude Code** dentro de `~/projetos`. |
| **Capacete** | Backend FastAPI para um ESP32 com microfone e buzzers: wake word "Jarvis", modo AFK, conversas com resumo. Veja [`helmet/README.md`](helmet/README.md). |

## Estrutura

```
jarvis-v2/
├── electron/                # app desktop
│   ├── main.js              # janela + sobe o backend como subprocesso
│   ├── preload.js           # ponte (sandboxed: só require('electron'), resto via IPC)
│   └── renderer/            # HUD (html/css/js, orbe, visualizações, vendor/ Chart.js + D3)
├── backend/
│   ├── server.py            # servidor WebSocket, proatividade, roteamento de mensagens
│   ├── jarvis/              # cérebro, STT/TTS, memória, tarefas, finanças, hábitos…
│   └── requirements.txt
├── helmet/                  # backend do capacete (FastAPI, venv própria) + firmware ESP32
├── scripts/
│   ├── jarvis               # launcher do desktop
│   └── jarvis-capacete      # launcher do capacete
├── .env.example
└── package.json
```

## Setup

Pré-requisitos: **Node 18+**, **Python 3.10+** com `python3-venv`. Para o oráculo/Claude
Code, a CLI `claude` instalada e logada (opcional).

```bash
# 1) Electron
npm install

# 2) Backend Python
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt

# 3) Segredos
cp .env.example .env     # preencha OPENAI_API_KEY (obrigatória) e as demais que quiser
```

### Modelos de voz

Ficam em `~/.jarvis/models/` (fora do repositório) e carregam em segundo plano após o
boot; a UI avisa "Voz pronta".

- **Whisper**: baixado automaticamente pelo `faster-whisper` no primeiro uso.
- **Kokoro**: coloque `kokoro-v1.0.onnx` e `voices-v1.0.bin` em `~/.jarvis/models/kokoro/`
  (releases do [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx/releases)).
- **Piper** (fallback):
  ```bash
  backend/.venv/bin/python -m piper.download_voices pt_BR-faber-medium \
    --data-dir ~/.jarvis/models/piper
  ```
- **XTTS-v2** (opcional): precisa de uma venv separada com PyTorch em `backend/.venv-xtts`
  com o pacote `coqui-tts` instalado.

## Rodar

```bash
npm start            # ou: npm run dev
```

Alias global:

```bash
echo "alias jarvis='$PWD/scripts/jarvis'" >> ~/.bashrc && source ~/.bashrc
jarvis
```

| Atalho | Ação |
|---|---|
| `Ctrl+Enter` | mostra/foca a janela (global) |
| `Ctrl+Shift` | alterna texto ⇄ voz |
| `Espaço` (segurar) | fala no modo voz (ou clique e segure o orbe) |

## Configuração

Os segredos ficam no `.env` (veja `.env.example`). As preferências ficam em
`~/.jarvis/config.json`, criado no primeiro boot com os padrões de
`backend/jarvis/config.py`. As principais:

| Chave | Padrão | Descrição |
|---|---|---|
| `ia_provider` | `openai` | `openai` ou `gemini` |
| `openai_model` | `gpt-4o-mini` | modelo do cérebro |
| `tts_engine` | `elevenlabs` | `kokoro` (recomendado: local e rápido), `piper`, `xtts` ou `elevenlabs` |
| `stt_model` | `small` | modelo do Whisper (`medium` = mais preciso, mais lento) |
| `proativo_intervalo_min` / `_max` | `30` / `60` | minutos entre iniciativas (`min <= 0` desativa) |
| `oraculo_enabled`, `perplexity_enabled`, `claude_code_enabled` | `true` | liga/desliga integrações |
| `tasks_enabled`, `finance_enabled`, `habits_enabled`, `second_brain_enabled`, `viz_enabled` | `true` | liga/desliga módulos |

Os dados (memória, banco SQLite, visualizações salvas) também ficam em `~/.jarvis/`.

## Capacete (ESP32)

Roda separado do desktop (porta 8770, venv `helmet/.venv`):

```bash
python3 -m venv helmet/.venv
helmet/.venv/bin/pip install -r helmet/requirements.txt
scripts/jarvis-capacete
```

No firmware, copie `helmet/firmware/capacete/segredos.example.h` para `segredos.h` e
preencha o Wi-Fi e o `HELMET_TOKEN`, que precisa ser o mesmo valor do `.env`. Protocolo,
simulador, testes e o passo a passo completo estão em [`helmet/README.md`](helmet/README.md).

## Licença

MIT

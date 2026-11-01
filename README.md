# ATLAS I

**A**utonomous **T**echnology, **L**ogic & **A**ssistance **S**ystem.

Meu assistente pessoal. A ideia veio do assistente do Homem de Ferro e foi crescendo:
hoje ele fala, escuta, lembra das coisas entre uma sessão e outra, puxa assunto do
nada, cuida das minhas tarefas, tempo, gastos e hábitos, faz gráfico quando eu peço
e ainda roda num capacete com ESP32 que eu montei.

Uso no Pop!_OS. A interface é em Electron e o cérebro é em Python, os dois conversando
por WebSocket local.

```
 Electron (HUD) ──WebSocket 127.0.0.1──► backend/ (Python)  ──► OpenAI · Perplexity · Claude
                                              │
 Capacete ESP32 ──Wi-Fi/WebSocket──► helmet/ (FastAPI) ──► usa o mesmo pacote atlas/
```

## O que ele faz

- **Dois modos de tela.** Um modo texto, parecido com um terminal, e um modo voz com
  um orbe em tela cheia que reage ao áudio. `Ctrl+Shift` troca entre eles.
- **Cérebro:** `gpt-4o-mini` com tool calling. Dá pra trocar pro Gemini pelo
  `ia_provider` no config.
- **Voz:** o STT é local, com `faster-whisper`. Pro TTS eu uso o Kokoro, mas também
  funciona com Piper, XTTS-v2 (bem mais natural, só que lento e precisa de venv
  separada) ou ElevenLabs.
- **Memória:** ele recarrega as últimas conversas quando inicia e guarda fatos sobre
  mim, tanto os que ele percebe sozinho quanto os que eu peço ("lembra que...").
- **Puxa assunto:** de tempos em tempos ele começa uma conversa sozinho. Não
  interrompe se eu estiver falando e respeita um horário de silêncio.
- **Produtividade:** tarefas e projetos, cronômetro de tempo por projeto, controle de
  gastos, hábitos e um "segundo cérebro" pra guardar ideias e buscar depois.
- **Gráficos:** gera visualizações em HTML com Chart.js/D3 na hora, e dá pra salvar e
  abrir de novo depois.
- **Coisas em tempo real** (cotação, clima, notícia): Perplexity Sonar.
- **Claude como oráculo:** quando o GPT não sabe alguma coisa ele pergunta pro Claude
  pela CLI `claude`. Também consegue mandar o Claude Code fazer coisas dentro de
  `~/projetos`.
- **Capacete:** um backend FastAPI pra um ESP32 com microfone e buzzers. Acorda
  quando escuta "Atlas", volta a dormir sozinho e guarda resumo das conversas.
  Mais detalhes em [`helmet/README.md`](helmet/README.md).

## Como tá organizado

```
atlasI/
├── electron/          # app desktop (janela, ponte IPC e o HUD em renderer/)
├── backend/
│   ├── server.py      # servidor WebSocket e a parte de puxar assunto
│   ├── atlas/         # cérebro, voz, memória, tarefas, finanças, hábitos...
│   └── requirements.txt
├── helmet/            # backend do capacete + firmware do ESP32
├── scripts/
│   ├── atlas          # abre o desktop
│   └── atlas-capacete # sobe o backend do capacete
├── .env.example
└── package.json
```

## Instalando

Precisa de Node 18+ e Python 3.10+ (com `python3-venv`). A CLI `claude` só é
necessária se quiser o oráculo e o Claude Code.

```bash
npm install

python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt

cp .env.example .env   # só a OPENAI_API_KEY é obrigatória
```

### Modelos de voz

Ficam em `~/.atlas/models/`, fora do repo. Eles carregam em segundo plano depois que
o app abre, e aparece "Voz pronta" quando terminam.

- Whisper: baixa sozinho na primeira vez.
- Kokoro: baixe o `kokoro-v1.0.onnx` e o `voices-v1.0.bin` dos
  [releases do kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx/releases) e
  coloque em `~/.atlas/models/kokoro/`.
- Piper (reserva, caso o Kokoro falhe):
  ```bash
  backend/.venv/bin/python -m piper.download_voices pt_BR-faber-medium \
    --data-dir ~/.atlas/models/piper
  ```
- XTTS-v2 é opcional e precisa de uma venv própria em `backend/.venv-xtts`, com
  PyTorch e `coqui-tts`.

## Rodando

```bash
npm start
```

Eu deixo um alias no `.bashrc` pra abrir de qualquer lugar:

```bash
echo "alias atlas='$PWD/scripts/atlas'" >> ~/.bashrc && source ~/.bashrc
atlas
```

Atalhos:

- `Ctrl+Enter` mostra a janela (funciona em qualquer lugar do sistema)
- `Ctrl+Shift` troca entre texto e voz
- segurar `Espaço` (ou clicar e segurar o orbe) pra falar no modo voz

## Configuração

As chaves de API ficam no `.env`. O resto fica em `~/.atlas/config.json`, que é
criado na primeira vez que roda, com os valores padrão de `backend/atlas/config.py`.
As opções que eu mais mexo:

| Chave | Padrão | Pra quê |
|---|---|---|
| `ia_provider` | `openai` | `openai` ou `gemini` |
| `openai_model` | `gpt-4o-mini` | modelo usado no cérebro |
| `tts_engine` | `elevenlabs` | `kokoro`, `piper`, `xtts` ou `elevenlabs` |
| `stt_model` | `small` | modelo do Whisper (`medium` erra menos, mas demora mais) |
| `proativo_intervalo_min` / `_max` | `30` / `60` | minutos entre uma puxada de assunto e outra (`min <= 0` desliga) |
| `oraculo_enabled`, `perplexity_enabled`, `claude_code_enabled` | `true` | integrações |
| `tasks_enabled`, `finance_enabled`, `habits_enabled`, `second_brain_enabled`, `viz_enabled` | `true` | módulos |

Memória, banco SQLite e gráficos salvos também ficam em `~/.atlas/`.

## Capacete

Roda separado do desktop, na porta 8770 e com venv própria:

```bash
python3 -m venv helmet/.venv
helmet/.venv/bin/pip install -r helmet/requirements.txt
scripts/atlas-capacete
```

No firmware, copie `helmet/firmware/capacete/segredos.example.h` pra `segredos.h` e
coloque o Wi-Fi e o `HELMET_TOKEN` (tem que ser igual ao do `.env`). Protocolo,
simulador, testes e o resto está em [`helmet/README.md`](helmet/README.md).

## Licença

MIT

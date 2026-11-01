# ATLAS — Capacete (ESP32 ⇄ FastAPI ⇄ ATLAS)

Backend que deixa o capacete do Homem de Ferro (ESP32 + microfone + 2 buzzers)
conversar por voz com o **mesmo ATLAS do desktop**: mesma persona, mesma memória
de fatos, mesmas ferramentas (tarefas, tempo, finanças, hábitos, segundo cérebro,
oráculo Claude, Perplexity em tempo real).

Roda **separado** do ATLAS desktop: tem processo, venv (`helmet/.venv`), porta
(8770) e banco (`~/.atlas/capacete.db`) próprios. Nada no `backend/` nem no
`electron/` foi alterado. Os dois podem rodar ao mesmo tempo.

---

## 1. Visão geral

```
 CAPACETE (ESP32)                         PC (hoje) / VPS (amanhã)
 ┌──────────────────────┐   Wi-Fi    ┌───────────────────────────────────────────────┐
 │ mic I2S ─► PCM 16 kHz├──binário──►│ /ws/device                                    │
 │                      │            │   VAD (webrtcvad) ─► enunciados               │
 │ buzzers ◄─ "tone"    │◄──JSON─────│   Whisper (STT local) ─► texto                │
 │ (amp I2S ◄─ PCM)     │◄──binário──│   "Atlas"? ─► máquina de estados (2 min AFK) │
 │ botão  ─► wake/sleep │──JSON─────►│   cérebro ATLAS (gpt-4o-mini + ferramentas)  │
 └──────────────────────┘            │   Kokoro (TTS) ─► voz (capacete OU caixas PC) │
                                     │   SQLite/Postgres: conversas + interações     │
                                     │   OpenAI: título/descrição ao encerrar        │
                                     │ /api/*  REST   ·  /ws/monitor eventos ao vivo │
                                     └───────────────────────────────────────────────┘
```

## 2. Decisões (e o porquê)

### Onde processar a voz: **no PC (backend), não no ESP32**

| Opção | Veredito |
|---|---|
| Transcrever no ESP32 | **Inviável.** Whisper precisa de centenas de MB de RAM e GFLOPs; o ESP32 tem ~512 KB de SRAM (8 MB de PSRAM no S3). Os reconhecedores que rodam nele (ESP-SR MultiNet) só reconhecem uma lista fixa de comandos, não fala livre em PT-BR. |
| Mandar áudio cru pro PC | **Escolhida.** PCM 16 kHz/16 bits = 32 KB/s (256 kbps): nada para o Wi-Fi local. O PC transcreve em ~1 s (`small`). Firmware fica simples (capturar e enviar). |

**Velocidade:** sem codec (Opus/ADPCM) no ESP32. Comprimir custa CPU no ESP e
latência no PC, e na rede local não há ganho. Na VPS, se o upload pesar, dá pra
adotar ADPCM 4:1 depois (o protocolo já tem o `hello` pra negociar isso).

### Wake word "Atlas": detectada no **texto**, no PC

Cada enunciado vira texto (Whisper `small`, ~1 s) e o backend procura "Atlas"
(com as variações que o Whisper produz: *átlas*, *atles*, *hatlas*…). A grande
vantagem: **"Atlas, que horas são?" numa frase só** ativa e já responde, sem
esperar um bipe entre o nome e o pedido.

Evolução possível (fase 2): wake word **no próprio ESP32-S3** com o ESP-SR/WakeNet
(confira se há um modelo "Atlas" para a sua placa). Aí o capacete só transmite
depois do nome, o que economiza banda e privacidade, útil na VPS. O backend não
muda: o firmware manda `{"type":"wake"}` e começa a transmitir.

### Resposta: **texto + voz + bipes**

- **Bipes** (sempre): os buzzers tocam sons de estado (ativou, entendi, dormiu,
  erro). O backend manda a "partitura" (`[[freq, ms], …]`), e o firmware só toca.
- **Texto** (sempre): `transcript` e `reply`, para log, display ou depuração.
- **Voz** em um de dois destinos (automático):
  - **Nas caixas do PC** se o capacete não tiver alto-falante (padrão hoje).
  - **No capacete** se o firmware declarar `speaker: true`: o backend faz stream
    do PCM no ritmo do tempo real.

> ⚠️ **Buzzer não reproduz voz.** Buzzer (sobretudo o ativo) só faz tons. Para o
> ATLAS falar *dentro* do capacete, adicione um **amplificador I2S MAX98357A
> (~R$ 15) + alto-falante 3 W 4 Ω (28–40 mm)**. Os buzzers continuam para os bipes.

### Cérebro: o **mesmo** do desktop, usado como biblioteca

`helmet/brain.py` importa o pacote `atlas` do `backend/` e monta o `OpenAIBrain`
com as mesmas ferramentas, exceto:
- **visualizações** e **troca de voz**: não há tela no capacete;
- **Claude Code** (executa código sem confirmação): desligado por padrão, porque o
  microfone fica aberto e qualquer um pode dizer "Atlas". Liga com `HELMET_ALLOW_CODE=true`.

A persona ganha um adendo de **canal de voz**: respostas curtas, sem markdown,
sem listas, sem emojis.

**Memória:** os **fatos** sobre você são compartilhados com o desktop (lidos do
`memoria.json`; os novos são *mesclados* de volta com trava de arquivo, sem tocar
no histórico do desktop). O **histórico** (contexto) do capacete vem do banco dele,
por conversa.

---

## 3. Regras de escuta e de conversa

```
            "Atlas" (ou botão)                    pedido
    IDLE ───────────────────────► LISTENING ─────────────► THINKING ──► SPEAKING
     ▲                                 ▲                                    │
     │   120 s de AFK real / "pode     └────────────────────────────────────┘
     └────────── desligar" ◄────────── (qualquer fala renova os 120 s)
```

**Escuta**
1. **Dormindo (IDLE):** tudo que é dito é transcrito, mas só "Atlas" faz algo.
   Nada é gravado no banco.
2. **"Atlas"** abre uma escuta ativa. "Atlas" sozinho: bipe e espera o pedido.
   "Atlas, X": bipe e responde X na hora.
3. **Ativo (LISTENING):** qualquer fala vira pedido (não precisa repetir "Atlas")
   e renova o prazo.
4. **AFK real de 120 s:** o relógio **só corre quando nada acontece**. Fica pausado
   enquanto você fala, enquanto há fala na fila e enquanto o ATLAS pensa ou fala.
   Ruído que o Whisper transcreve como vazio **não** renova o prazo.
5. **Dormir antes:** "pode desligar", "é só isso", "valeu, pode dormir"… ou o
   botão (`{"type":"sleep"}`).
6. **Queda de Wi-Fi:** o estado é por `device_id`, não por conexão. Se o capacete
   reconectar dentro do prazo, a escuta continua.
7. **Microfone mudo enquanto o ATLAS fala** (para ele não ouvir a si mesmo). Por
   isso não dá pra interromper a resposta falando por cima (*barge-in*): é uma
   evolução futura.

**Conversas (banco)**
1. Toda interação (sua fala + resposta + métricas de latência) é gravada na
   **conversa aberta** do capacete.
2. A conversa atravessa várias escutas até o tamanho (texto UTF-8) atingir
   `HELMET_CONVERSATION_MAX_BYTES` (padrão 20 000, ~60–100 trocas).
3. **Estourou no meio de uma escuta ao vivo? Continua na mesma conversa** até a
   escuta terminar (os 2 min de AFK). Só então ela é encerrada.
4. Conversa encerrada vai para a OpenAI, que gera **título + descrição curtos**
   (em segundo plano, com até 3 tentativas, retomadas no boot se o PC cair).
5. A próxima escuta abre uma conversa nova, e o cérebro começa com contexto limpo.
6. Reinício/queda do backend: escutas abertas são fechadas (`restart`) e as regras
   aplicadas. O contexto da conversa aberta é recarregado do banco.

**Modelo de dados** (`helmet/db.py`):
`devices` ─< `conversations` ─< `listen_sessions` ─< `interactions`

---

## 4. Latência (medida neste PC, 12 núcleos, sem GPU)

| Etapa | Tempo | Observação |
|---|---|---|
| Fim da fala detectado (VAD) | 0,6 s | `HELMET_VAD_END_SILENCE_MS`. Menor = mais rápido, mas corta pausas no meio da frase |
| STT Whisper `small` (padrão) | ~1,0–1,2 s | `base` ≈ 0,45 s, mas com ruído errou o próprio "Atlas" (ouviu "Já vi") |
| Cérebro gpt-4o-mini | 0,6–1,3 s | conexão aquecida enquanto você fala; picos de 2 s+ quando usa ferramentas |
| 1º pedaço de voz (Kokoro) | 0,3–0,6 s | resposta falada frase a frase; o 1º pedaço é curto de propósito |
| **Fim da fala → 1ª palavra** | **~2,3–3,2 s** (`small`) · **~1,6–2,5 s** (`base`) | + 0,6 s do VAD |

**Teste de ruído** (fala sintetizada + ruído branco forte, `simulador.py --noise 900`):

| Frase | `base` | `small` |
|---|---|---|
| Atlas, qual a previsão do tempo para amanhã? | "**Já vi** qual a previsão…" (não acorda) | ✔ |
| E a da Argentina? | "E a dar gente se mexe." | ✔ |
| Quanto eu gastei com mercado esse mês? | ✔ | ✔ |

Por isso o padrão é `small`. Com o mic real já montado, teste o `base`
(`HELMET_STT_MODEL=base HELMET_STT_WAKE_MODEL=base`): se acertar no seu
capacete, ganha ~0,7 s por resposta.

Otimizações já aplicadas: Whisper com beam 1 e sem VAD duplicado; conexão
HTTPS com a OpenAI aquecida no início da sua fala; TTS em pipeline (sintetiza a
frase 2 enquanto toca a 1); primeiro pedaço ≤ 80 caracteres.

Próximos ganhos possíveis: resposta do GPT em *streaming* (falar enquanto ele
escreve, uns −0,5 s) e GPU para o Whisper (`small` em <0,2 s).

---

## 5. Protocolo do capacete (contrato com o firmware)

**Conexão:** `ws://<PC>:8770/ws/device?device_id=capacete-01&token=<HELMET_TOKEN>`
(também aceita `Authorization: Bearer <token>`). Token errado: fecha com código 4401.

**Achar o PC:** mDNS anuncia `atlas.local` e o serviço `_atlas-helmet._tcp`
(porta + `path=/ws/device`). No ESP32: `MDNS.queryHost("atlas")`. Alternativa:
IP fixo reservado no roteador.

### ESP32 → backend

| Mensagem | Quando |
|---|---|
| **binário**: PCM s16le mono, na taxa `mic_rate` | o tempo todo; pacotes de 20–60 ms (640–1920 bytes a 16 kHz) |
| `{"type":"hello","fw":"0.1.0","mic_rate":16000,"speaker":false,"speaker_rate":16000,"display":{"cols":16,"rows":2,"ascii":true}}` | logo após conectar (`display` é **opcional**: sem ele = sem tela, comportamento original) |
| `{"type":"playback_done","seq":N}` | (com alto-falante) terminou de tocar o áudio `seq` |
| `{"type":"wake"}` / `{"type":"sleep"}` | botão físico (ativa sem falar / dorme) |
| `{"type":"ping","t":123}` | opcional (o servidor já faz ping de WebSocket a cada 20 s) |

### Backend → ESP32

| Mensagem | Firmware faz |
|---|---|
| `{"type":"welcome","audio_target":"pc"\|"device"\|"none","active_timeout_s":120,"display":{...}\|null,...}` | — (vale o welcome que chega **depois** do hello) |
| `{"type":"state","state":"idle"\|"listening"\|"thinking"\|"speaking","active":true,"expires_in":120}` | LEDs/olhos do capacete, se quiser |
| `{"type":"tone","name":"wake","notes":[[880,70],[0,30],[1319,70]],"buzzer":"both"\|0\|1}` | **toca a sequência** (freq 0 = pausa) |
| `{"type":"transcript","text":"..."}` / `{"type":"reply","text":"..."}` | log/display (opcional) |
| `{"type":"audio_start","seq":N,"rate":16000,"format":"s16le","channels":1}` | prepara o I2S de saída |
| **binário**: PCM da resposta | escreve no I2S (buffer de ~0,5 s basta: o backend manda no ritmo real, no máx. 400 ms à frente) |
| `{"type":"audio_end","seq":N,"duration_ms":2300}` | ao esvaziar o buffer, manda `playback_done` |
| `{"type":"status","text":"..."}` / `{"type":"error","error":"..."}` | log |
| `{"type":"display","text":"Feito.","lines":["Feito."],"ttl_ms":8000,"kind":"reply"\|"status"}` | **desenha `lines`** (só com display declarado) |

### Display I2C (LCD 16x2 / 20x4, OLED ~21x5)

Declarado no `hello` com `"display":{"cols":C,"rows":R,"ascii":true}`
(cols 8–64, rows 1–16; `ascii` padrão `true`). Valor inválido = `error` + sem display.
O `welcome` seguinte confirma o que o servidor entendeu:

```json
"display": {"cols":16,"rows":2,"ascii":true,"max_chars":32,"ttl_ms":8000,"reply_style":"display"}
```

- `max_chars` = `min(cols*rows, HELMET_DISPLAY_MAX_CHARS)` (teto 60): limite pedido ao GPT.
- `reply_style`: `display` (sem alto-falante: o cérebro responde UMA frase curta,
  como "Oi!", "Estou aqui.", "Enviado.", "Feito.", "Nao consegui.") ou `voice` (com
  alto-falante: resposta no estilo falado, e a tela mostra a versão cortada).

Mensagem `display` (backend → ESP32), **além** do `reply` (que não muda e traz o texto completo):

```json
{"type":"display","text":"Agora sao 14:30,","lines":["Agora sao 14:30,"],"ttl_ms":8000,"kind":"reply"}
```

| Campo | Garantia |
|---|---|
| `lines` | 1..`rows` strings, cada uma com **≤ `cols`** caracteres, já quebradas por palavra. Desenhe linha `i` na linha `i` da tela; apague o resto. |
| `text` | `lines` unidas por espaço (para log/serial). |
| `ascii` | se `true`, **só ASCII 32–126** (acentos dobrados: á→a, ç→c; aspas curvas → `"`; travessão → `-`; `…` → `...`). Se `false`, UTF-8 (a fonte do OLED precisa ter os glifos). |
| corte | se não coube, termina em fim de palavra com `...` (nunca no meio; só uma palavra maior que a linha inteira é partida). Números e horas ficam intactos. |
| `ttl_ms` | quanto tempo manter na tela; depois apague (ou volte à tela de repouso). **`0` = até o próximo quadro.** |
| `kind` | `reply` (resposta do ATLAS, ttl `HELMET_DISPLAY_TTL_MS`), `status` (`"Ouvindo..."` ao ativar, ttl 0; `"Em espera."` ao dormir, ttl 3000). |

Um quadro novo sempre substitui o anterior. Se o cérebro falhar, o `reply` traz o
texto de erro falado de sempre, e o display mostra `Nao consegui.`.

Bipes definidos em `helmet/tones.py`: `ready` (conectou), `wake` (ativou), `ack`
(entendi, pensando), `sleep` (dormiu), `error`. Mude os sons ali, sem regravar o ESP32.

---

## 6. Plano do firmware (para você implementar)

**Hardware recomendado**
- **ESP32-S3** (com PSRAM): I2S melhor, USB nativo, e abre caminho pro WakeNet.
  O ESP32 clássico também serve para esta arquitetura.
- **Microfone I2S MEMS INMP441** (ou ICS-43434). Evite microfone analógico com ADC
  do ESP32: muito ruído.
- **Buzzers:** prefira **passivos** (tocam frequências via LEDC/`tone()`). Buzzer
  ativo só faz um tom fixo (dá pra fazer os bipes como pulsos liga/desliga).
- (Opcional, para voz no capacete) **MAX98357A + alto-falante 3 W 4 Ω**.
- Posicione o mic **perto da boca e longe do alto-falante**. Espuma ajuda contra
  o "eco" do capacete fechado.

**Ligações sugeridas (ESP32-S3)**: INMP441 SCK→GPIO4, WS→GPIO5, SD→GPIO6, L/R→GND;
MAX98357A BCLK→GPIO15, LRC→GPIO16, DIN→GPIO17; buzzer 0→GPIO9, buzzer 1→GPIO10
(com transistor se o buzzer puxar >20 mA); botão→GPIO0/qualquer com pull-up.

**Estrutura (Arduino-ESP32 ou ESP-IDF, FreeRTOS)**
1. **Wi-Fi** com reconexão automática; resolve `atlas.local` (mDNS) ou IP fixo.
2. **WebSocket** (lib `links2004/arduinoWebSockets` ou `esp_websocket_client`) com
   reconexão a cada 2 s. Ao conectar, manda `hello`.
3. **Task do mic** (core 1): `i2s_read` em blocos de 20–40 ms. O INMP441 entrega
   amostras de 24 bits em slots de 32 bits, então converta com `(int16_t)(s >> 14)`
   (ajuste o shift/ganho até a fala ficar forte sem clipar). Envie via `sendBIN`.
   **Mande sempre** (o backend decide o que é fala); se o WS caiu, descarte.
4. **Task dos bipes**: fila de `tone`; toca as notas com `ledcWriteTone`.
   `"both"` = os dois buzzers juntos.
5. **(Com alto-falante)** Task de saída: ring buffer de ~1 s; `audio_start`
   configura a taxa do I2S; os binários vão ao buffer; com `audio_end` e o buffer
   vazio, envia `playback_done`.
6. **Botão**: clique = `wake`; segurar 1 s = `sleep`.
7. **LED/estado** (opcional) a partir de `state`.

**Teste do firmware sem o backend completo:** `helmet/tools/simulador.py` é a
referência do protocolo. O firmware deve se comportar igual a ele.

---

## 7. Rodar

```bash
# 1x: venv própria (já criada)
python3 -m venv helmet/.venv && helmet/.venv/bin/pip install -r helmet/requirements.txt

# subir (porta 8770, todas as interfaces da rede local)
scripts/atlas-capacete
```

O token do firmware é o `HELMET_TOKEN` do `.env` da raiz (já gerado). Se o
firewall estiver ativo: `sudo ufw allow 8770/tcp`.

**Testar sem o capacete** (simulador = mesmo protocolo do ESP32):
```bash
# falando no microfone do PC
helmet/.venv/bin/python helmet/tools/simulador.py --mic
# frases sintetizadas, sem falar
helmet/.venv/bin/python helmet/tools/simulador.py --say "Atlas, que horas são?"
# simulando capacete com alto-falante (áudio volta pelo WebSocket e toca no PC)
helmet/.venv/bin/python helmet/tools/simulador.py --speaker --play --say "Atlas, bom dia"
# simulando capacete com display I2C (desenha cada quadro numa caixinha ASCII)
helmet/.venv/bin/python helmet/tools/simulador.py --display 16x2 --say "Atlas, oi"
```

**Testes automatizados** (91 testes: wake word, VAD, regras de conversa, AFK,
limite, resumo, reconexão, stream de áudio, auth, display, inglês, filtros do mic):
```bash
cd helmet && .venv/bin/python -m pytest -q
```

**Iniciar com o sistema** (systemd de usuário), em `~/.config/systemd/user/atlas-capacete.service`:
```ini
[Unit]
Description=ATLAS capacete
After=network-online.target sound.target

[Service]
ExecStart=%h/projetos/atlasI/scripts/atlas-capacete
Restart=on-failure

[Install]
WantedBy=default.target
```
Depois: `systemctl --user enable --now atlas-capacete`.

## 8. API

Auth: `Authorization: Bearer <HELMET_TOKEN>` (ou `?token=`). Documentação
interativa em `http://localhost:8770/docs`.

| Método | Rota | O quê |
|---|---|---|
| GET | `/health` | status, modelos prontos (público) |
| GET | `/api/devices` | capacetes: conectado, estado, segundos até dormir |
| POST | `/api/devices/{id}/text` `{"text","force_active"}` | manda texto como se fosse fala |
| POST | `/api/devices/{id}/wake` · `/sleep` | ativa / dorme na marra |
| GET | `/api/conversations?device_id=&status=open\|closed&limit=&offset=` | lista conversas |
| GET | `/api/conversations/{id}` | conversa + escutas |
| GET | `/api/conversations/{id}/interactions` | trocas com métricas (stt/cérebro/tts/total ms) |
| POST | `/api/conversations/{id}/finalize` | encerra manualmente (409 se houver escuta ao vivo) |
| POST | `/api/conversations/{id}/summarize` | refaz título/descrição |
| WS | `/ws/monitor` | eventos ao vivo (fala, transcrição, estados, resumos) |

## 9. Configuração (`.env` da raiz, prefixo `HELMET_`)

| Variável | Padrão | |
|---|---|---|
| `HELMET_TOKEN` | (gerado) | segredo compartilhado com o firmware |
| `HELMET_PORT` | 8770 | |
| `HELMET_ACTIVE_TIMEOUT_S` | 120 | AFK até dormir |
| `HELMET_CONVERSATION_MAX_BYTES` | 20000 | limite da conversa |
| `HELMET_REPLY_AUDIO` | auto | `auto` \| `device` \| `pc` \| `none`. Capacete só com display (sem alto-falante): `none` deixa o PC calado |
| `HELMET_STT_MODEL` / `HELMET_STT_WAKE_MODEL` | small / small | `base` = −0,7 s, erra mais com ruído |
| `HELMET_VAD_END_SILENCE_MS` | 600 | silêncio que fecha a frase |
| `HELMET_VAD_AGGRESSIVENESS` | 2 | 0–3; suba se o ruído do capacete disparar falsas falas |
| `HELMET_VAD_MIN_RMS` | 200 | piso de energia (ajuste ao ganho do mic) |
| `HELMET_ALLOW_CODE` | false | Claude Code por voz |
| `HELMET_BRAIN_MODEL` | (vazio) | modelo OpenAI só do capacete; vazio = o do desktop |
| `HELMET_DATABASE_URL` | sqlite `~/.atlas/capacete.db` | |
| `HELMET_LANGUAGE` | pt | `pt` \| `en`. `en`: você fala inglês, ele responde em inglês britânico no estilo do filme (o desktop não muda) |
| `HELMET_TTS_VOICE` / `HELMET_TTS_LANG` | (padrão do idioma) | voz Kokoro do capacete; `en` usa `bm_daniel` / `en-gb` |
| `HELMET_INPUT_HIGHPASS_HZ` | 200 | passa-alta antes do VAD/Whisper (tira zumbido 60/120 Hz do mic); 0 desliga |
| `HELMET_VAD_NOISE_FACTOR` | 3.0 | fala só conta acima de N x o ruído de fundo medido; 0 desliga |
| `HELMET_DEBUG_AUDIO_DIR` | (vazio) | diagnóstico do mic: salva cada frase em WAV + o stream bruto, e loga a taxa real recebida |
| `HELMET_DISPLAY_MAX_CHARS` | 60 | teto da resposta no canal de display (limite = min(cols*rows, teto)) |
| `HELMET_DISPLAY_TTL_MS` | 8000 | tempo da resposta na tela (`0` = até o próximo quadro) |
| `HELMET_DISPLAY_STATUS` | true | quadros "Ouvindo..." / "Em espera." |
| `HELMET_DISPLAY_SLEEP_TTL_MS` | 3000 | tempo do "Em espera." |

## 10. Migrar para VPS

- `HELMET_DATABASE_URL=postgresql+asyncpg://…` (instale `asyncpg`). As tabelas são
  criadas no boot.
- **TLS obrigatório:** coloque atrás de um Caddy/Nginx e use `wss://` no firmware.
- `HELMET_REPLY_AUDIO=device` (na VPS não há caixa de som).
- CPU da VPS: Whisper `base` precisa de ~2 vCPUs para ~0,5 s. Alternativa: trocar
  o STT por API (OpenAI `gpt-4o-mini-transcribe`): a interface é só `transcribe_pcm()`.
- Ferramentas que dependem do PC (Claude Code, `~/.atlas/atlas.db` local) passam
  a ser da VPS. Os fatos e o banco de tarefas teriam que ser migrados junto.
- Considere a wake word no ESP32-S3 (seção 2) para não transmitir áudio 24 h pela internet.

## 11. Limitações conhecidas

- **Sem barge-in:** não dá pra interromper o ATLAS falando por cima.
- **Fatos novos vs. desktop:** se o desktop estiver aberto e aprender um fato, ele
  regrava o `memoria.json` com a lista que tem em memória, e pode descartar um fato
  que o capacete aprendeu nesse meio-tempo. O contrário não acontece: o capacete
  sempre mescla. Corrigir exige uma mudança pequena no `backend/atlas/memory.py`
  (mesclar ao salvar), que não foi feita para não mexer no desktop.
- Latência dominada por CPU (Whisper `small` ~1 s). Com GPU NVIDIA, o Whisper cai
  para ~0,2 s (`device="cuda"` em `audio/stt.py`).
- **Display:** o banco grava a resposta COMPLETA; só a tela recebe a versão
  cortada. O GPT às vezes passa do limite: aí o corte (fim de palavra + `...`) entra,
  sem segunda chamada. Sem alto-falante, o áudio ainda sai no PC em `auto` (o TTS
  roda igual); use `HELMET_REPLY_AUDIO=none` para silenciar. Sem rolagem: texto
  que não cabe é cortado, não paginado.
- **Hora:** o prompt do desktop não traz a hora; o capacete injeta data/hora
  atuais no adendo de canal a cada pedido (senão "que horas são?" falha).
- Um usuário por vez: vários capacetes funcionam (estado e conversas por
  `device_id`), mas todos compartilham os mesmos fatos e ferramentas.

## 12. Mapa do código

```
helmet/
├── run.py                  # entrada (uvicorn)
├── requirements.txt        # venv própria
├── helmet/
│   ├── settings.py         # config HELMET_* (+ .env da raiz)
│   ├── app.py              # FastAPI: boot, carga de modelos, mDNS, DI p/ testes
│   ├── api/routes.py       # /ws/device, /ws/monitor, REST, auth
│   ├── runtime.py          # ★ máquina de estados por capacete (escuta, AFK, fala)
│   ├── conversations.py    # ★ regras de conversa/limite/resumo/recuperação
│   ├── db.py               # modelos SQLAlchemy (async)
│   ├── wake.py             # "Atlas" + comandos de dormir
│   ├── brain.py            # ponte com o cérebro do desktop + memória de fatos + adendo voz/display
│   ├── display.py          # LCD/OLED: quebra em cols x rows, ASCII, corte com "..."
│   ├── summarizer.py       # título/descrição via OpenAI
│   ├── tones.py            # bipes dos buzzers
│   ├── discovery.py        # mDNS
│   ├── events.py           # barramento p/ /ws/monitor
│   └── audio/
│       ├── vad.py          # segmentação de fala (webrtcvad)
│       ├── stt.py          # Whisper sobre PCM cru + filtro de alucinações
│       ├── tts.py          # Kokoro, frase a frase
│       ├── playback.py     # caixas do PC (paplay)
│       └── resample.py
├── tools/simulador.py      # capacete virtual (referência do protocolo)
├── firmware/capacete/      # firmware do ESP32 (Arduino): capacete.ino + segredos.h (Wi-Fi/token, NÃO compartilhe)
└── tests/                  # pytest (servidor real em thread + fakes)
```

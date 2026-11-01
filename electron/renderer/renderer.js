'use strict';

/**
 * Renderer do ATLAS — dois mundos visuais:
 *   · modo TEXTO = terminal estilo Claude Code (log + prompt de entrada)
 *   · modo VOZ   = orbe pulsante fullscreen (áudio-reativo via Web Audio)
 * Ctrl+Shift alterna entre eles. O protocolo WebSocket com o backend não mudou.
 */

// ---------- elementos ----------
const appEl = document.getElementById('app');
const connEl = document.getElementById('conn');
const connLabel = document.getElementById('conn-label');
const modeBadge = document.getElementById('mode-badge');

const logEl = document.getElementById('log');
const composerEl = document.getElementById('composer');
const inputEl = document.getElementById('input');
const micEl = document.getElementById('mic');
const hintDot = document.getElementById('hint-dot');
const hintText = document.getElementById('hint-text');
const clockEl = document.getElementById('clock');

const voiceViewEl = document.getElementById('voice-view');
const voiceHintEl = document.getElementById('voice-hint');
const orbCanvas = document.getElementById('orb');

// ---------- estado ----------
let ws = null;
let reconnectTimer = null;
let mode = 'text'; // 'text' | 'voice'
let state = 'booting';
let voiceReady = false;

const STATE_LABEL = {
  booting: 'iniciando…',
  offline: 'offline',
  idle: 'pronto',
  listening: 'ouvindo…',
  thinking: 'processando…',
  speaking: 'falando…',
};

// ---------- Web Audio ----------
let audioCtx = null;
function ensureAudio() {
  if (!audioCtx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    audioCtx = new AC();
  }
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
}

// ---------- gravação (mic) ----------
let mediaRecorder = null;
let recChunks = [];
let recording = false;
let micStream = null;
let micNode = null;

// ---------- reprodução (TTS) ----------
let currentAudio = null;
let currentSrcNode = null;

// ============================================================
//  ESTADO VISUAL
// ============================================================
function setState(s) {
  state = STATE_LABEL[s] ? s : 'idle';
  appEl.dataset.state = state;
  Orb.setState(state);

  // linha de status (modo texto)
  hintDot.className = 'hint-dot ' + (state === 'thinking' ? 'thinking' : state === 'offline' ? '' : 'idle');
  hintText.textContent = STATE_LABEL[state];

  // dica (modo voz)
  updateVoiceHint();
}

function updateVoiceHint() {
  if (!voiceReady) {
    voiceHintEl.textContent = 'CARREGANDO VOZ';
    return;
  }
  if (state === 'listening') voiceHintEl.textContent = 'ESCUTANDO';
  else if (state === 'thinking') voiceHintEl.textContent = 'PROCESSANDO';
  else if (state === 'speaking') voiceHintEl.textContent = 'RESPONDENDO';
  else voiceHintEl.textContent = 'SEGURE ESPAÇO PARA FALAR';
}

function setConnection(online) {
  connEl.classList.toggle('conn--on', online);
  connEl.classList.toggle('conn--off', !online);
  connLabel.textContent = online ? 'online' : 'offline';
  if (!online) {
    hideThinking();
    setState('offline');
  }
}

// ============================================================
//  MODO TEXTO — log de turnos
// ============================================================
const TURN_NAMES = { user: 'LUCAS', atlas: 'ATLAS' };

function pad2(n) { return String(n).padStart(2, '0'); }
function stamp() {
  const d = new Date();
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

// Revela um texto caractere a caractere (efeito terminal) no corpo do turno.
// Timer local por elemento: várias respostas podem digitar sem se atrapalhar.
function typeInto(bodyEl, text) {
  const total = text.length;
  const dur = Math.min(1500, Math.max(280, total * 13));
  const perTick = Math.max(1, Math.ceil(total / (dur / 16)));
  let i = 0;
  bodyEl.classList.add('typing');
  const timer = setInterval(() => {
    i += perTick;
    bodyEl.textContent = text.slice(0, i);
    logEl.scrollTop = logEl.scrollHeight;
    if (i >= total) {
      clearInterval(timer);
      bodyEl.textContent = text;
      bodyEl.classList.remove('typing');
    }
  }, 16);
}

// Monta um turno: nó no trilho + cabeçalho (hora + nome) + corpo.
function buildTurn(who) {
  const turn = document.createElement('div');
  turn.className = `turn turn--${who}`;

  const node = document.createElement('span');
  node.className = 'node';
  const content = document.createElement('div');
  content.className = 'content';

  if (who !== 'system') {
    const head = document.createElement('div');
    head.className = 'turn-head';
    const tm = document.createElement('span');
    tm.className = 'turn-time';
    tm.textContent = stamp();
    const nm = document.createElement('span');
    nm.className = 'turn-name';
    nm.textContent = TURN_NAMES[who] || who.toUpperCase();
    head.appendChild(tm);
    head.appendChild(nm);
    content.appendChild(head);
  }
  const body = document.createElement('div');
  body.className = 'turn-body';
  content.appendChild(body);

  turn.appendChild(node);
  turn.appendChild(content);
  logEl.appendChild(turn);
  return { turn, body };
}

function addTurn(who, text) {
  const { body } = buildTurn(who);
  if (who === 'atlas') {
    typeInto(body, text); // respostas do ATLAS surgem digitando
  } else {
    body.textContent = text;
    logEl.scrollTop = logEl.scrollHeight;
  }
}

function showThinking() {
  hideThinking();
  const { turn, body } = buildTurn('atlas');
  turn.id = 'thinking';
  body.innerHTML =
    '<span class="thinking-dots"><span></span><span></span><span></span></span>';
  logEl.scrollTop = logEl.scrollHeight;
}
function hideThinking() {
  const el = document.getElementById('thinking');
  if (el) el.remove();
}

// ============================================================
//  WEBSOCKET
// ============================================================
function connect() {
  ws = new WebSocket(window.atlas.backendUrl);

  ws.addEventListener('open', () => {
    setConnection(true);
    setState('idle');
    clearTimeout(reconnectTimer);
    send({ type: 'ping' });
    send({ type: 'mode', payload: { mode } }); // backend precisa saber p/ proatividade
  });

  ws.addEventListener('message', (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleMessage(msg);
  });

  ws.addEventListener('close', () => {
    setConnection(false);
    scheduleReconnect();
  });

  ws.addEventListener('error', () => ws.close());
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, 1200);
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
    return true;
  }
  return false;
}

function handleMessage(msg) {
  const p = msg.payload || {};
  switch (msg.type) {
    case 'hello':
      addTurn('system', p.text || 'Backend pronto.');
      setState('idle');
      if (p.voice_ready) enableVoice();
      break;
    case 'pong':
      break;
    case 'voice_ready':
      enableVoice();
      break;
    case 'transcript':
      addTurn('user', p.text);
      break;
    case 'status':
      if (p.state === 'thinking') setState('thinking');
      break;
    case 'message':
      hideThinking();
      addTurn('atlas', p.text);
      if (p.info) {
        // Mensagem intermediária (ex: Claude Code em execução) — mantém orbe em
        // 'thinking' com aura laranja pulsante e reexibe indicador no modo texto.
        setState('thinking');
        showThinking();
      } else if (state !== 'speaking') {
        // Sem áudio a seguir, volta para idle; se vier 'speech', ele assume.
        setState('idle');
      }
      break;
    case 'speech':
      playSpeech(p.audio_b64);
      break;
    case 'visualization':
      // O desenho é da 2ª janela; aqui só garantimos que ela ABRA (mesmo que
      // tenha sido fechada). Ao (re)conectar, o backend reenvia o último design.
      if (window.atlas.showViz) window.atlas.showViz();
      break;
    default:
      console.warn('mensagem desconhecida:', msg);
  }
}

// ============================================================
//  VOZ — disponibilidade
// ============================================================
function enableVoice() {
  if (voiceReady) return;
  voiceReady = true;
  micEl.disabled = false;
  updateVoiceHint();
  addTurn('system', 'Voz pronta — segure ● (ou espaço, no modo voz) para falar.');
}

// ============================================================
//  VOZ — reprodução da fala do ATLAS (com analyser p/ o orbe)
// ============================================================
function playSpeech(audioB64) {
  if (!audioB64) return;
  stopPlayback();
  try {
    const ctx = ensureAudio();
    const audio = new Audio(`data:audio/wav;base64,${audioB64}`);
    const srcNode = ctx.createMediaElementSource(audio);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.75;
    srcNode.connect(analyser);
    analyser.connect(ctx.destination);

    currentAudio = audio;
    currentSrcNode = srcNode;
    setState('speaking');
    Orb.attachAnalyser(analyser);

    const done = () => {
      if (currentAudio === audio) {
        Orb.detachAnalyser();
        currentAudio = null;
        currentSrcNode = null;
        setState('idle');
      }
    };
    audio.addEventListener('ended', done);
    audio.play().catch((err) => {
      console.warn('falha ao tocar áudio:', err);
      done();
    });
  } catch (err) {
    console.warn('áudio inválido:', err);
    Orb.detachAnalyser();
    setState('idle');
  }
}

function stopPlayback() {
  if (currentAudio) {
    try { currentAudio.pause(); } catch {}
    currentAudio = null;
    currentSrcNode = null;
  }
}

// ============================================================
//  VOZ — gravação do microfone (alta qualidade)
// ============================================================
async function startRecording() {
  if (recording || !voiceReady) return;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: 48000,
        sampleSize: 16,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (err) {
    console.warn('sem acesso ao microfone:', err);
    addTurn('system', 'Não consegui acessar o microfone, senhor.');
    return;
  }

  // Analyser do mic alimenta o orbe enquanto ouve (não vai ao destino: sem eco).
  try {
    const ctx = ensureAudio();
    micNode = ctx.createMediaStreamSource(micStream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.7;
    micNode.connect(analyser);
    Orb.attachAnalyser(analyser);
  } catch (err) {
    console.warn('analyser do mic falhou:', err);
  }

  const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : 'audio/webm';
  recChunks = [];
  mediaRecorder = new MediaRecorder(micStream, {
    mimeType: mime,
    audioBitsPerSecond: 128000, // opus 128k: voz nítida p/ o Whisper
  });
  mediaRecorder.addEventListener('dataavailable', (e) => {
    if (e.data && e.data.size > 0) recChunks.push(e.data);
  });
  mediaRecorder.addEventListener('stop', () => {
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    if (micNode) { try { micNode.disconnect(); } catch {} micNode = null; }
    Orb.detachAnalyser();
    micStream = null;
    sendRecording();
  });
  mediaRecorder.start();
  recording = true;
  micEl.classList.add('recording');
  setState('listening');
}

function stopRecording() {
  if (!recording) return;
  recording = false;
  micEl.classList.remove('recording');
  if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
}

async function sendRecording() {
  const blob = new Blob(recChunks, { type: 'audio/webm' });
  if (blob.size < 1200) {
    // Toque acidental / muito curto.
    setState('idle');
    return;
  }
  setState('thinking');
  if (mode === 'text') showThinking();
  const b64 = await blobToBase64(blob);
  const ok = send({
    type: 'audio',
    payload: { audio_b64: b64, speak: mode === 'voice' },
  });
  if (!ok) {
    hideThinking();
    addTurn('system', 'sem conexão com o backend.');
    setState('offline');
  }
}

function blobToBase64(blob) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(String(reader.result).split(',')[1] || '');
    reader.readAsDataURL(blob);
  });
}

// ============================================================
//  ENVIO DE TEXTO
// ============================================================
composerEl.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;
  addTurn('user', text);
  inputEl.value = '';
  composerEl.classList.remove('has-text');
  setState('thinking');
  showThinking();
  const ok = send({ type: 'message', payload: { text, speak: mode === 'voice' } });
  if (!ok) {
    hideThinking();
    addTurn('system', 'sem conexão com o backend.');
    setState('offline');
  }
});

// Cursor de bloco: mostra quando o campo está vazio, some ao digitar.
inputEl.addEventListener('input', () => {
  composerEl.classList.toggle('has-text', inputEl.value.length > 0);
});

// ============================================================
//  ALTERNÂNCIA DE MODO (Ctrl+Shift)
// ============================================================
function setMode(m) {
  mode = m;
  appEl.dataset.mode = m;
  modeBadge.textContent = m === 'voice' ? 'voz' : 'texto';
  if (window.atlas.setVoiceMode) window.atlas.setVoiceMode(m === 'voice');
  send({ type: 'mode', payload: { mode: m } });
  if (m === 'voice') {
    ensureAudio();
    Orb.start();
    updateVoiceHint();
    if (document.activeElement) document.activeElement.blur();
  } else {
    Orb.stop();
    inputEl.focus();
  }
}

function toggleMode() {
  setMode(mode === 'text' ? 'voice' : 'text');
}

// Chord Ctrl+Shift: dispara ao soltar, se nenhuma outra tecla entrou no meio.
let ctrlDown = false, shiftDown = false, otherPressed = false, chordArmed = false;
window.addEventListener('keydown', (e) => {
  if (e.key === 'Control' || e.key === 'Shift') {
    if (e.key === 'Control') ctrlDown = true;
    else shiftDown = true;
    if (ctrlDown && shiftDown) {
      chordArmed = !otherPressed; // ambos pressionados: arma se nada poluiu
    } else {
      otherPressed = false; // 1º modificador desce → nova janela limpa
      chordArmed = false;
    }
  } else {
    otherPressed = true; // uma tecla não-modificadora poluiu o chord
    chordArmed = false;
  }
});
window.addEventListener('keyup', (e) => {
  if (e.key !== 'Control' && e.key !== 'Shift') return;
  const fire = chordArmed;
  if (e.key === 'Control') ctrlDown = false;
  else shiftDown = false;
  if (fire) toggleMode();
  chordArmed = false;
  if (!ctrlDown && !shiftDown) otherPressed = false;
});

// ============================================================
//  SEGURAR PARA FALAR
// ============================================================
// Modo texto: botão de microfone.
micEl.addEventListener('pointerdown', (e) => { e.preventDefault(); startRecording(); });
micEl.addEventListener('pointerup', stopRecording);
micEl.addEventListener('pointerleave', stopRecording);

// Modo voz: clique/segura no orbe.
voiceViewEl.addEventListener('pointerdown', (e) => {
  if (mode !== 'voice') return;
  e.preventDefault();
  startRecording();
});
window.addEventListener('pointerup', () => { if (mode === 'voice') stopRecording(); });
window.addEventListener('pointercancel', () => { if (mode === 'voice') stopRecording(); });

// Modo voz: barra de espaço (segurar).
window.addEventListener('keydown', (e) => {
  if (mode === 'voice' && e.code === 'Space' && !e.repeat) {
    e.preventDefault();
    startRecording();
  }
});
window.addEventListener('keyup', (e) => {
  if (mode === 'voice' && e.code === 'Space') {
    e.preventDefault();
    stopRecording();
  }
});

// ============================================================
//  CONTROLES DE JANELA
// ============================================================
document.getElementById('btn-hide').addEventListener('click', () => window.atlas.hideWindow());
document.getElementById('btn-quit').addEventListener('click', () => window.atlas.quit());
// Abre a 2ª janela com o último design (o backend reenvia ao (re)conectar).
document.getElementById('btn-viz').addEventListener('click', () => window.atlas.showViz && window.atlas.showViz());

// ============================================================
//  RELÓGIO DO CONSOLE
// ============================================================
function tickClock() { clockEl.textContent = stamp(); }
tickClock();
setInterval(tickClock, 1000);

// ============================================================
//  BOOT
// ============================================================
Orb.init(orbCanvas);
setMode('text');
setConnection(false);
setState('booting');
addTurn('system', 'ATLAS I — inicializando interface.');
connect();
inputEl.focus();

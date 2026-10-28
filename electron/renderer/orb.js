'use strict';

/**
 * Orbe do modo voz — visualizador HUD tecnológico em <canvas>.
 *
 * Núcleo luminoso (ciano→água) reativo ao áudio, cercado por uma instrumentação
 * "de sistema": anéis concêntricos finos girando em velocidades diferentes,
 * arcos/segmentos de leitura, marcas radiais (ticks), partículas orbitando e
 * barras de frequência. Reage ao áudio real quando há um AnalyserNode ligado
 * (mic ao ouvir, TTS ao falar); sem áudio, sintetiza o movimento por estado.
 *
 * API global:
 *   Orb.init(canvas)
 *   Orb.start() / Orb.stop()
 *   Orb.setState('idle'|'listening'|'thinking'|'speaking')
 *   Orb.attachAnalyser(analyser) / Orb.detachAnalyser()
 */
const Orb = (() => {
  const CYAN   = [0, 212, 255];
  const AQUA   = [0, 255, 200];
  const ORANGE = [255, 130, 0];
  const NBARS = 108;
  const NPART = 22;

  let canvas = null, ctx = null, dpr = 1, W = 0, H = 0;
  let raf = null, lastTs = 0, t = 0;

  let state = 'idle';
  let analyser = null, freq = null;

  let level = 0;      // amplitude suavizada 0..1
  let spinScale = 0.5;
  let rBars = 0, rA = 0, rB = 0, rC = 0, rD = 0; // acumuladores de rotação
  let ripples = [], beatCd = 0;
  let particles = [];

  const lerp = (a, b, x) => a + (b - a) * x;
  const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
  const mix = (c1, c2, x) => [
    Math.round(lerp(c1[0], c2[0], x)),
    Math.round(lerp(c1[1], c2[1], x)),
    Math.round(lerp(c1[2], c2[2], x)),
  ];
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

  function init(el) {
    canvas = el;
    ctx = canvas.getContext('2d');
    for (let i = 0; i < NPART; i++) {
      particles.push({
        ang: Math.random() * Math.PI * 2,
        band: 1.75 + Math.random() * 0.95,        // raio (× base)
        spd: (0.25 + Math.random() * 0.5) * (Math.random() < 0.5 ? -1 : 1),
        size: 0.9 + Math.random() * 1.6,
        aqua: Math.random() < 0.5,
        phase: Math.random() * Math.PI * 2,
      });
    }
    resize();
    window.addEventListener('resize', resize);
  }

  function resize() {
    if (!canvas) return;
    dpr = window.devicePixelRatio || 1;
    const r = canvas.getBoundingClientRect();
    W = Math.max(1, r.width);
    H = Math.max(1, r.height);
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function setState(s) { state = s; }
  function attachAnalyser(a) { analyser = a; freq = new Uint8Array(a.frequencyBinCount); }
  function detachAnalyser() { analyser = null; freq = null; }

  function readLevel() {
    if (analyser) {
      analyser.getByteFrequencyData(freq);
      let sum = 0;
      const n = Math.min(freq.length, 48);
      for (let i = 2; i < n; i++) sum += freq[i];
      return clamp01((sum / ((n - 2) * 255)) * 1.7);
    }
    if (state === 'speaking') return 0.5 + 0.35 * Math.abs(Math.sin(t * 5));
    if (state === 'thinking') return 0.32 + 0.22 * Math.abs(Math.sin(t * 6.5));
    if (state === 'listening') return 0.16 + 0.08 * Math.sin(t * 2.2);
    return 0.11 + 0.05 * Math.sin(t * 1.1);
  }

  function barValue(i) {
    const half = NBARS / 2;
    const idx = i < half ? i : NBARS - i;
    if (analyser && freq) return (freq[2 + Math.floor((idx / half) * 52)] || 0) / 255;
    const speed = state === 'speaking' ? 6 : state === 'thinking' ? 7 : state === 'listening' ? 2.5 : 1.4;
    const amp = state === 'speaking' ? 0.7 : state === 'thinking' ? 0.55 : state === 'listening' ? 0.32 : 0.2;
    return amp * (0.45 + 0.55 * Math.abs(Math.sin(idx * 0.4 + t * speed)));
  }

  function frame(ts) {
    const dt = lastTs ? Math.min(0.05, (ts - lastTs) / 1000) : 0.016;
    lastTs = ts;
    t += dt;

    level = lerp(level, readLevel(), 0.28);

    const targetSpin =
      state === 'thinking' ? 2.3 : state === 'speaking' ? 1.5 : state === 'listening' ? 0.75 : 0.5;
    spinScale = lerp(spinScale, targetSpin, 0.05);
    const ds = dt * spinScale;
    rBars += ds * 0.22;
    rA += ds * 0.5;
    rB -= ds * 0.33;
    rC += ds * 0.8;
    rD -= ds * 0.18;
    for (const p of particles) p.ang += p.spd * ds;

    beatCd -= dt;
    if ((state === 'speaking' || state === 'listening') && level > 0.55 && beatCd <= 0) {
      ripples.push({ r: 0, a: 0.45 });
      beatCd = 0.16;
    }

    draw();
    raf = requestAnimationFrame(frame);
  }

  // helper: anel segmentado (arcos com lacunas) girando
  function arcRing(cx, cy, r, rot, segs, gap, color, alpha, lw) {
    const span = (Math.PI * 2) / segs;
    ctx.lineWidth = lw;
    ctx.strokeStyle = rgba(color, alpha);
    for (let i = 0; i < segs; i++) {
      const a0 = rot + i * span + gap / 2;
      const a1 = rot + (i + 1) * span - gap / 2;
      ctx.beginPath();
      ctx.arc(cx, cy, r, a0, a1);
      ctx.stroke();
    }
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2;
    const unit = Math.min(W, H);
    const base = unit * 0.13;
    const cCore = mix(CYAN, AQUA, clamp01(0.25 + level * 0.5));

    // ---- brilho de fundo ----
    const glowR = base * (2.8 + level * 1.4);
    const bg = ctx.createRadialGradient(cx, cy, base * 0.2, cx, cy, glowR);
    bg.addColorStop(0, rgba(cCore, 0.22 + level * 0.22));
    bg.addColorStop(0.5, rgba(CYAN, 0.05));
    bg.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, W, H);

    // ---- aura laranja: Claude Code em processamento — fundo ambiente ----
    if (state === 'thinking') {
      const pA = 0.5 + 0.5 * Math.sin(t * 2.6);
      const auraGrad = ctx.createRadialGradient(cx, cy, base * 1.2, cx, cy, unit * 0.72);
      auraGrad.addColorStop(0,    'rgba(255,130,0,0)');
      auraGrad.addColorStop(0.55, rgba(ORANGE, 0.045 + pA * 0.038));
      auraGrad.addColorStop(1,    'rgba(255,80,0,0)');
      ctx.fillStyle = auraGrad;
      ctx.fillRect(0, 0, W, H);
    }

    // ---- marcas radiais (ticks) — "escala de instrumento" ----
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(rD);
    const rTick = base * 1.44;
    for (let i = 0; i < 72; i++) {
      const a = (i / 72) * Math.PI * 2;
      const major = i % 6 === 0;
      const len = major ? 7 : 3.5;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * rTick, Math.sin(a) * rTick);
      ctx.lineTo(Math.cos(a) * (rTick + len), Math.sin(a) * (rTick + len));
      ctx.strokeStyle = rgba(major ? AQUA : CYAN, major ? 0.5 : 0.18);
      ctx.lineWidth = major ? 1.6 : 1;
      ctx.stroke();
    }
    ctx.restore();

    // ---- anéis concêntricos finos (velocidades diferentes) ----
    // fino tracejado
    ctx.save();
    ctx.setLineDash([2, 8]);
    arcRing(cx, cy, base * 1.7, rA, 1, 0, CYAN, 0.28, 1);
    ctx.setLineDash([]);
    ctx.restore();
    // arcos de leitura (3 segmentos) girando ao contrário
    arcRing(cx, cy, base * 2.02, rB, 3, 0.9, AQUA, 0.4, 1.4);
    // anel externo quase completo (2 segmentos amplos)
    arcRing(cx, cy, base * 2.34, rC, 2, 0.6, CYAN, 0.22, 1);
    // par de arcos curtos externos (leituras)
    arcRing(cx, cy, base * 2.6, rD * 1.6, 4, 2.35, AQUA, 0.3, 1.2);

    // ---- anéis de batida ----
    for (let i = ripples.length - 1; i >= 0; i--) {
      const rp = ripples[i];
      rp.r += unit * 0.006;
      rp.a *= 0.955;
      if (rp.a < 0.02) { ripples.splice(i, 1); continue; }
      ctx.beginPath();
      ctx.arc(cx, cy, base * 1.5 + rp.r, 0, Math.PI * 2);
      ctx.strokeStyle = rgba(cCore, rp.a);
      ctx.lineWidth = 1.6;
      ctx.stroke();
    }

    // ---- partículas orbitando ----
    for (const p of particles) {
      const rr = base * p.band + Math.sin(t * 1.3 + p.phase) * base * 0.06;
      const x = cx + Math.cos(p.ang) * rr;
      const y = cy + Math.sin(p.ang) * rr;
      const col = p.aqua ? AQUA : CYAN;
      const a = 0.35 + 0.4 * (0.5 + 0.5 * Math.sin(t * 2 + p.phase)) + level * 0.2;
      ctx.beginPath();
      ctx.arc(x, y, p.size, 0, Math.PI * 2);
      ctx.fillStyle = rgba(col, clamp01(a));
      ctx.shadowColor = rgba(col, 0.8);
      ctx.shadowBlur = 6;
      ctx.fill();
    }
    ctx.shadowBlur = 0;

    // ---- barras de frequência radiais ----
    const rInner = base * 1.3;
    const barMax = unit * 0.14;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(rBars);
    ctx.lineCap = 'round';
    for (let i = 0; i < NBARS; i++) {
      const ang = (i / NBARS) * Math.PI * 2;
      const v = barValue(i);
      const len = 2 + v * barMax;
      const cBar = mix(CYAN, AQUA, clamp01(0.2 + v));
      ctx.beginPath();
      ctx.moveTo(Math.cos(ang) * rInner, Math.sin(ang) * rInner);
      ctx.lineTo(Math.cos(ang) * (rInner + len), Math.sin(ang) * (rInner + len));
      ctx.strokeStyle = rgba(cBar, 0.5 + v * 0.45);
      ctx.lineWidth = 1.8;
      ctx.stroke();
    }
    ctx.restore();

    // ---- anéis pulsantes laranja: Claude Code em ação ----
    if (state === 'thinking') {
      const p1 = 0.5 + 0.5 * Math.sin(t * 3.4);
      const p2 = 0.5 + 0.5 * Math.sin(t * 3.4 + Math.PI * 0.65);
      const p3 = 0.5 + 0.5 * Math.sin(t * 2.2 + Math.PI * 1.3);
      ctx.save();

      // anel interno — pulsante e intenso
      ctx.beginPath();
      ctx.arc(cx, cy, base * (1.5 + p1 * 0.2), 0, Math.PI * 2);
      ctx.strokeStyle = rgba(ORANGE, 0.18 + p1 * 0.52);
      ctx.lineWidth = 2.2;
      ctx.shadowColor = rgba(ORANGE, 0.9);
      ctx.shadowBlur = 12 + p1 * 26;
      ctx.stroke();

      // anel médio — fase defasada
      ctx.beginPath();
      ctx.arc(cx, cy, base * (2.05 + p2 * 0.18), 0, Math.PI * 2);
      ctx.strokeStyle = rgba(ORANGE, 0.08 + p2 * 0.28);
      ctx.lineWidth = 1.4;
      ctx.shadowColor = rgba(ORANGE, 0.65);
      ctx.shadowBlur = 8 + p2 * 16;
      ctx.stroke();

      // anel externo tênue
      ctx.beginPath();
      ctx.arc(cx, cy, base * (2.65 + p3 * 0.14), 0, Math.PI * 2);
      ctx.strokeStyle = rgba(ORANGE, 0.04 + p3 * 0.1);
      ctx.lineWidth = 1;
      ctx.shadowBlur = 0;
      ctx.stroke();

      ctx.restore();
    }

    // ---- núcleo luminoso ----
    const R = base * (1 + level * 0.5);
    ctx.save();
    ctx.shadowColor = rgba(cCore, 0.9);
    ctx.shadowBlur = 38 + level * 55;
    const core = ctx.createRadialGradient(cx - R * 0.22, cy - R * 0.22, R * 0.1, cx, cy, R);
    core.addColorStop(0, rgba(mix(cCore, [255, 255, 255], 0.7), 1));
    core.addColorStop(0.5, rgba(cCore, 0.95));
    core.addColorStop(1, rgba(mix(cCore, CYAN, 0.5), 0.4));
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    // aro fino em volta do núcleo
    ctx.beginPath();
    ctx.arc(cx, cy, R * 1.1, 0, Math.PI * 2);
    ctx.strokeStyle = rgba(mix(cCore, [255, 255, 255], 0.4), 0.3 + level * 0.4);
    ctx.lineWidth = 1.2;
    ctx.stroke();
  }

  function start() {
    if (raf) return;
    resize();
    lastTs = 0;
    raf = requestAnimationFrame(frame);
  }
  function stop() { if (raf) cancelAnimationFrame(raf); raf = null; }

  return { init, start, stop, setState, attachAnalyser, detachAnalyser };
})();

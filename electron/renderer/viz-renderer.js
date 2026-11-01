'use strict';
/*
 * Renderer da 2ª janela (tela de visualizações). Recebe specs normalizados do
 * backend via WebSocket (mensagem `visualization`) e os desenha SEM dependências
 * externas: SVG para gráficos/mapa mental, DOM para kpis/lista. Mesmo DNA HUD do
 * resto do ATLAS (ciano/água sobre fundo quase-preto).
 *
 * Expõe `window.Viz.render(spec)` para permitir verificação offscreen.
 */
(function () {
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const PALETTE = ['#00D4FF', '#00FFC8', '#0EA5E9', '#06B6D4', '#38bdf8', '#5eead4', '#818cf8', '#22d3ee'];
  const ATLAS = '#00D4FF', AGUA = '#00FFC8', DIM = '#64748b', TEXT = '#E2E8F0';

  const $stage = () => document.getElementById('stage');
  const $title = () => document.getElementById('title');

  function el(tag, attrs, text) {
    const n = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }
  function svgRoot() {
    const s = $stage();
    const w = s.clientWidth || 900, h = s.clientHeight || 480;
    const svg = el('svg', { viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet' });
    svg._w = w; svg._h = h;
    return svg;
  }
  function fmt(v, unidade) {
    const n = typeof v === 'number' ? v.toLocaleString('pt-BR', { maximumFractionDigits: 2 }) : v;
    if (!unidade) return String(n);
    if (unidade === 'R$') return `R$ ${n}`;
    if (unidade === '%') return `${n}%`;
    return `${n} ${unidade}`;
  }

  function render(spec) {
    if (!spec || !spec.tipo) return;
    $title().textContent = spec.titulo || 'Visualização';
    const stage = $stage();
    stage.innerHTML = '';
    const draw = {
      html: renderHtml,
      barras: renderBarras, linhas: renderLinhas, pizza: renderPizza,
      lista: renderLista, kpis: renderKpis, mapa_mental: renderMapa,
    }[spec.tipo];
    if (draw) draw(spec, stage);
  }

  // ---- design livre (HTML gerado pelo ATLAS, isolado em iframe) ----------
  async function renderHtml(spec, stage) {
    const frame = document.createElement('iframe');
    // sandbox SEM allow-same-origin: origem opaca, sem acesso à página principal
    // nem ao preload (window.atlas). allow-scripts libera JS/Canvas/animações.
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.style.cssText = 'width:100%;height:100%;border:0;background:transparent;display:block;';
    // Bibliotecas pedidas (Chart.js/D3) injetadas INLINE, antes do HTML do modelo.
    // A fonte vem do processo main via IPC (o renderer é sandboxed); nada de rede.
    let libs = '';
    if (Array.isArray(spec.libs) && window.atlas && window.atlas.libSource) {
      for (const nome of spec.libs) {
        try {
          const src = await window.atlas.libSource(nome);
          if (src) libs += '<script>' + src + '</scr' + 'ipt>';
        } catch (_e) { /* segue sem a lib */ }
      }
    }
    // srcdoc (não data: URL — este trava o Chromium com origem opaca no Electron).
    // O filho herda o CSP DESTA página, que libera inline p/ o design ter JS/SVG.
    frame.srcdoc =
      '<!DOCTYPE html><html><head><meta charset="utf-8">' +
      '<style>html,body{margin:0;height:100%;background:#0a0a0f;color:#E2E8F0;' +
      "font-family:'Segoe UI',system-ui,sans-serif;overflow:auto}*{box-sizing:border-box}</style>" +
      libs + '</head><body>' + (spec.html || '') + '</body></html>';
    stage.appendChild(frame);
  }

  // ---- gráficos de barras -------------------------------------------------
  function renderBarras(spec, stage) {
    const svg = svgRoot();
    const W = svg._w, H = svg._h, padB = 46, padT = 26, padX = 30;
    const dados = spec.dados;
    const max = Math.max(...dados.map((d) => d.valor), 1);
    const plotH = H - padB - padT, plotW = W - padX * 2;
    const bw = plotW / dados.length;
    // linha de base
    svg.appendChild(el('line', { x1: padX, y1: H - padB, x2: W - padX, y2: H - padB, class: 'axis' }));
    dados.forEach((d, i) => {
      const bh = (d.valor / max) * plotH;
      const x = padX + i * bw + bw * 0.2, y = H - padB - bh, w = bw * 0.6;
      const id = `g${i}`;
      const grad = el('linearGradient', { id, x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.appendChild(el('stop', { offset: '0%', 'stop-color': PALETTE[i % PALETTE.length] }));
      grad.appendChild(el('stop', { offset: '100%', 'stop-color': PALETTE[i % PALETTE.length], 'stop-opacity': 0.25 }));
      svg.appendChild(grad);
      svg.appendChild(el('rect', { x, y, width: w, height: bh, rx: 4, fill: `url(#${id})`,
        stroke: PALETTE[i % PALETTE.length], 'stroke-opacity': 0.6 }));
      svg.appendChild(el('text', { x: x + w / 2, y: y - 8, class: 'val-label', 'text-anchor': 'middle' },
        fmt(d.valor, spec.unidade)));
      svg.appendChild(el('text', { x: x + w / 2, y: H - padB + 20, class: 'bar-label', 'text-anchor': 'middle' },
        trunc(d.rotulo, 14)));
    });
    stage.appendChild(svg);
  }

  // ---- gráfico de linhas --------------------------------------------------
  function renderLinhas(spec, stage) {
    const svg = svgRoot();
    const W = svg._w, H = svg._h, padB = 46, padT = 26, padX = 40;
    const dados = spec.dados;
    // Série temporal: eixo Y dá ZOOM no intervalo [min,max] (com folga) em vez de
    // começar em 0 — senão variações pequenas (ex.: câmbio 5,36→5,42) somem.
    const vals = dados.map((d) => d.valor);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi === lo) { hi += 1; lo -= 1; }
    const folga = (hi - lo) * 0.18; lo -= folga; hi += folga;
    const plotH = H - padB - padT, plotW = W - padX * 2;
    const step = dados.length > 1 ? plotW / (dados.length - 1) : 0;
    const yOf = (v) => H - padB - ((v - lo) / (hi - lo)) * plotH;
    const pts = dados.map((d, i) => [padX + i * step, yOf(d.valor)]);
    svg.appendChild(el('line', { x1: padX, y1: H - padB, x2: W - padX, y2: H - padB, class: 'axis' }));
    // área sob a linha
    const area = `M${pts[0][0]},${H - padB} ` + pts.map((p) => `L${p[0]},${p[1]}`).join(' ') +
      ` L${pts[pts.length - 1][0]},${H - padB} Z`;
    const gid = 'lg';
    const grad = el('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(el('stop', { offset: '0%', 'stop-color': ATLAS, 'stop-opacity': 0.35 }));
    grad.appendChild(el('stop', { offset: '100%', 'stop-color': ATLAS, 'stop-opacity': 0 }));
    svg.appendChild(grad);
    svg.appendChild(el('path', { d: area, fill: `url(#${gid})` }));
    svg.appendChild(el('polyline', { points: pts.map((p) => p.join(',')).join(' '),
      fill: 'none', stroke: ATLAS, 'stroke-width': 2.5 }));
    // Com muitos pontos (ex.: 30 dias), afina rótulos p/ não amontoar: mostra ~8.
    const n = pts.length;
    const passo = n > 12 ? Math.ceil(n / 8) : 1;
    const rPonto = n > 24 ? 2.5 : 4;
    pts.forEach((p, i) => {
      svg.appendChild(el('circle', { cx: p[0], cy: p[1], r: rPonto, fill: AGUA }));
      const mostrarRotulo = i % passo === 0 || i === n - 1;
      if (mostrarRotulo) {
        // valor só nos extremos quando há muitos pontos (evita poluir)
        if (n <= 12 || i === 0 || i === n - 1)
          svg.appendChild(el('text', { x: p[0], y: p[1] - 12, class: 'val-label', 'text-anchor': 'middle' },
            fmt(dados[i].valor, spec.unidade)));
        svg.appendChild(el('text', { x: p[0], y: H - padB + 20, class: 'bar-label', 'text-anchor': 'middle' },
          trunc(dados[i].rotulo, 12)));
      }
    });
    stage.appendChild(svg);
  }

  // ---- pizza / donut ------------------------------------------------------
  function renderPizza(spec, stage) {
    const svg = svgRoot();
    const W = svg._w, H = svg._h;
    const dados = spec.dados;
    const total = dados.reduce((s, d) => s + d.valor, 0) || 1;
    const cx = W * 0.34, cy = H / 2, r = Math.min(W * 0.3, H * 0.42);
    let a0 = -Math.PI / 2;
    dados.forEach((d, i) => {
      const a1 = a0 + (d.valor / total) * Math.PI * 2;
      const large = a1 - a0 > Math.PI ? 1 : 0;
      const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
      const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
      svg.appendChild(el('path', {
        d: `M${cx},${cy} L${x0},${y0} A${r},${r} 0 ${large} 1 ${x1},${y1} Z`,
        fill: PALETTE[i % PALETTE.length], 'fill-opacity': 0.85,
        stroke: '#0a0a0f', 'stroke-width': 2,
      }));
      a0 = a1;
    });
    // furo do donut + total no centro
    svg.appendChild(el('circle', { cx, cy, r: r * 0.55, fill: '#0a0a0f' }));
    svg.appendChild(el('text', { x: cx, y: cy - 4, 'text-anchor': 'middle', fill: '#fff',
      'font-size': 22, 'font-weight': 700 }, fmt(round2(total), spec.unidade)));
    svg.appendChild(el('text', { x: cx, y: cy + 18, 'text-anchor': 'middle', fill: DIM, 'font-size': 12 }, 'total'));
    // legenda à direita
    const lx = W * 0.66, ly = cy - dados.length * 15;
    dados.forEach((d, i) => {
      const y = ly + i * 30;
      svg.appendChild(el('rect', { x: lx, y: y - 10, width: 14, height: 14, rx: 3, fill: PALETTE[i % PALETTE.length] }));
      const pct = Math.round((d.valor / total) * 100);
      svg.appendChild(el('text', { x: lx + 24, y: y + 2, fill: TEXT, 'font-size': 14 },
        `${trunc(d.rotulo, 18)}  ·  ${fmt(d.valor, spec.unidade)} (${pct}%)`));
    });
    stage.appendChild(svg);
  }

  // ---- kpis (cards DOM) ---------------------------------------------------
  function renderKpis(spec, stage) {
    const wrap = document.createElement('div');
    wrap.className = 'cards';
    spec.cards.forEach((c) => {
      const card = document.createElement('div');
      card.className = 'card';
      const rot = document.createElement('div'); rot.className = 'rot'; rot.textContent = c.rotulo;
      const val = document.createElement('div'); val.className = 'val'; val.textContent = c.valor;
      card.appendChild(rot); card.appendChild(val);
      if (c.sub) { const s = document.createElement('div'); s.className = 'sub'; s.textContent = c.sub; card.appendChild(s); }
      wrap.appendChild(card);
    });
    stage.appendChild(wrap);
  }

  // ---- lista (DOM) --------------------------------------------------------
  function renderLista(spec, stage) {
    const ul = document.createElement('ul');
    ul.className = 'lista';
    spec.itens.forEach((it) => {
      const li = document.createElement('li');
      const dot = document.createElement('span'); dot.className = 'dot';
      const txt = document.createElement('span'); txt.className = 'txt'; txt.textContent = it.texto;
      li.appendChild(dot); li.appendChild(txt);
      if (it.detalhe) { const d = document.createElement('span'); d.className = 'det'; d.textContent = it.detalhe; li.appendChild(d); }
      ul.appendChild(li);
    });
    stage.appendChild(ul);
  }

  // ---- mapa mental (SVG radial) -------------------------------------------
  function renderMapa(spec, stage) {
    const svg = svgRoot();
    const W = svg._w, H = svg._h, cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.3;
    const ramos = spec.mapa.ramos;
    // nó central
    svg.appendChild(el('circle', { cx, cy, r: 46, fill: 'rgba(0,212,255,0.12)', stroke: ATLAS, 'stroke-width': 2 }));
    svg.appendChild(el('text', { x: cx, y: cy + 5, 'text-anchor': 'middle', fill: '#fff',
      'font-size': 15, 'font-weight': 700 }, trunc(spec.mapa.centro, 16)));
    ramos.forEach((ramo, i) => {
      const ang = (i / ramos.length) * Math.PI * 2 - Math.PI / 2;
      const rx = cx + R * Math.cos(ang), ry = cy + R * Math.sin(ang);
      const cor = PALETTE[i % PALETTE.length];
      svg.appendChild(el('line', { x1: cx + 46 * Math.cos(ang), y1: cy + 46 * Math.sin(ang),
        x2: rx, y2: ry, stroke: cor, 'stroke-width': 2, 'stroke-opacity': 0.7 }));
      svg.appendChild(el('rect', { x: rx - 62, y: ry - 16, width: 124, height: 32, rx: 8,
        fill: 'rgba(0,255,200,0.08)', stroke: cor, 'stroke-width': 1.5 }));
      svg.appendChild(el('text', { x: rx, y: ry + 5, 'text-anchor': 'middle', fill: TEXT,
        'font-size': 13, 'font-weight': 600 }, trunc(ramo.titulo, 14)));
      // filhos (pequenos, além do ramo)
      (ramo.filhos || []).forEach((f, j) => {
        const fr = R + 66 + j * 26;
        const spread = (j - (ramo.filhos.length - 1) / 2) * 0.22;
        const fa = ang + spread;
        const fx = cx + fr * Math.cos(fa), fy = cy + fr * Math.sin(fa);
        svg.appendChild(el('circle', { cx: fx, cy: fy, r: 3, fill: cor }));
        svg.appendChild(el('text', {
          x: fx + (Math.cos(ang) >= 0 ? 8 : -8), y: fy + 4,
          'text-anchor': Math.cos(ang) >= 0 ? 'start' : 'end', fill: DIM, 'font-size': 11,
        }, trunc(f, 16)));
      });
    });
    stage.appendChild(svg);
  }

  function trunc(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function round2(v) { return Math.round(v * 100) / 100; }

  // ---- WebSocket ----------------------------------------------------------
  let ws = null, reconnect = null;
  function connect() {
    if (!window.atlas || !window.atlas.backendUrl) return;
    ws = new WebSocket(window.atlas.backendUrl);
    ws.addEventListener('message', (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'visualization' && msg.payload && msg.payload.spec) {
        render(msg.payload.spec);
        if (window.atlas.showViz) window.atlas.showViz(); // 2ª janela aparece
      }
    });
    ws.addEventListener('close', () => { clearTimeout(reconnect); reconnect = setTimeout(connect, 1200); });
    ws.addEventListener('error', () => ws.close());
  }

  window.Viz = { render };
  window.addEventListener('DOMContentLoaded', connect);
})();

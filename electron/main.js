'use strict';

const { app, BrowserWindow, ipcMain, globalShortcut } = require('electron');
const path = require('path');
const net = require('net');
const { spawn } = require('child_process');
const fs = require('fs');

const ROOT = path.join(__dirname, '..');
const IS_DEV = process.argv.includes('--dev');

let mainWindow = null;
let vizWindow = null;
let backendProc = null;
let backendPort = null;

/** Descobre uma porta TCP livre em 127.0.0.1. */
function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/** Caminho do interpretador Python: usa a venv do projeto se existir. */
function resolvePython() {
  if (process.env.JARVIS_PYTHON) return process.env.JARVIS_PYTHON;
  const venvPy = path.join(ROOT, 'backend', '.venv', 'bin', 'python');
  return fs.existsSync(venvPy) ? venvPy : 'python3';
}

/** Sobe o backend Python e resolve quando ele sinaliza que está pronto. */
function startBackend(port) {
  return new Promise((resolve) => {
    const script = path.join(ROOT, 'backend', 'server.py');
    const python = resolvePython();

    backendProc = spawn(python, [script, '--port', String(port)], {
      cwd: ROOT,
      env: { ...process.env },
    });

    let settled = false;
    const ready = () => {
      if (!settled) {
        settled = true;
        resolve();
      }
    };

    backendProc.stdout.on('data', (chunk) => {
      const text = chunk.toString();
      process.stdout.write(`[backend] ${text}`);
      if (text.includes('JARVIS_BACKEND_READY')) ready();
    });
    backendProc.stderr.on('data', (chunk) => {
      process.stderr.write(`[backend:err] ${chunk}`);
    });
    backendProc.on('exit', (code, signal) => {
      console.log(`[backend] encerrado (code=${code} signal=${signal})`);
      backendProc = null;
    });

    // Fallback: não trava a UI se o "ready" não chegar.
    setTimeout(ready, 8000);
  });
}

function stopBackend() {
  if (backendProc) {
    backendProc.kill('SIGTERM');
    backendProc = null;
  }
}

/** Libera o microfone para o conteúdo local (getUserMedia). */
function grantMicPermission() {
  const { session } = require('electron');
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((_wc, permission, callback) => {
    callback(permission === 'media' || permission === 'audioCapture');
  });
  ses.setPermissionCheckHandler((_wc, permission) => {
    return permission === 'media' || permission === 'audioCapture';
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 860,
    height: 580,
    frame: false,
    transparent: true,
    resizable: true,
    skipTaskbar: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      additionalArguments: [`--jarvis-port=${backendPort}`],
    },
  });

  // Acima de tudo, inclusive de janelas fullscreen.
  mainWindow.setAlwaysOnTop(true, 'screen-saver');
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // Posiciona no topo-centro da tela.
  const { screen } = require('electron');
  const area = screen.getPrimaryDisplay().workAreaSize;
  const x = Math.round((area.width - 860) / 2);
  mainWindow.setPosition(x, 32);

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  if (IS_DEV) mainWindow.webContents.openDevTools({ mode: 'detach' });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

/**
 * 2ª janela: tela de visualizações dinâmicas. Fica no monitor secundário quando
 * há um; senão, num card grande deslocado no primário. Criada uma vez (oculta) e
 * conectada ao backend — aparece sozinha quando chega uma visualização.
 */
function createVizWindow() {
  const { screen } = require('electron');
  const displays = screen.getAllDisplays();
  const primary = screen.getPrimaryDisplay();
  const target = displays.find((d) => d.id !== primary.id) || primary;
  const b = target.workArea;
  const w = Math.min(1000, b.width - 80);
  const h = Math.min(680, b.height - 80);

  vizWindow = new BrowserWindow({
    width: w,
    height: h,
    x: Math.round(b.x + (b.width - w) / 2),
    y: Math.round(b.y + (b.height - h) / 2),
    frame: false,
    transparent: false,
    backgroundColor: '#0a0a0f',
    resizable: true,
    show: false,
    skipTaskbar: false,
    title: 'JARVIS · Visualizações',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      additionalArguments: [`--jarvis-port=${backendPort}`],
    },
  });
  vizWindow.loadFile(path.join(__dirname, 'renderer', 'viz.html'));
  if (IS_DEV) vizWindow.webContents.openDevTools({ mode: 'detach' });
  vizWindow.on('closed', () => { vizWindow = null; });
}

function showVizWindow() {
  if (!vizWindow) createVizWindow();
  if (vizWindow.isMinimized()) vizWindow.restore();
  vizWindow.show();
}

// --- IPC vindo do renderer (controles da janela) ---
ipcMain.on('window:hide', () => mainWindow && mainWindow.hide());
ipcMain.on('window:quit', () => app.quit());
ipcMain.on('viz:show', showVizWindow);
ipcMain.on('viz:hide', () => vizWindow && vizWindow.hide());

// Serve o código-fonte das bibliotecas de visualização (Chart.js/D3) ao renderer
// (que é sandboxed e não pode ler o disco). O renderer injeta inline no iframe.
const VIZ_LIBS = { chartjs: 'chart.umd.min.js', d3: 'd3.min.js' };
const _vizLibCache = {};
ipcMain.handle('viz:lib', (_e, nome) => {
  if (!(nome in VIZ_LIBS)) return '';
  if (_vizLibCache[nome] === undefined) {
    try {
      _vizLibCache[nome] = fs.readFileSync(
        path.join(__dirname, 'renderer', 'vendor', VIZ_LIBS[nome]), 'utf-8'
      );
    } catch (e) {
      console.error('[viz] falha ao ler lib', nome, e.message);
      _vizLibCache[nome] = '';
    }
  }
  return _vizLibCache[nome];
});

// Modo voz = orbe em tela cheia (cobre o monitor inteiro); texto restaura o
// card flutuante. Usamos bounds do display em vez de setFullScreen: mais
// confiável para janela frameless/transparente e sem a animação do SO.
let savedBounds = null;
ipcMain.on('window:voice-mode', (_e, on) => {
  if (!mainWindow) return;
  const { screen } = require('electron');
  if (on) {
    savedBounds = mainWindow.getBounds();
    const disp = screen.getDisplayNearestPoint(mainWindow.getBounds());
    mainWindow.setBounds(disp.bounds); // monitor inteiro (inclui painéis)
  } else if (savedBounds) {
    mainWindow.setBounds(savedBounds);
    savedBounds = null;
  }
});

app.whenReady().then(async () => {
  backendPort = await getFreePort();
  grantMicPermission();
  await startBackend(backendPort);
  createWindow();
  // 2ª janela já conectada ao backend (oculta); aparece na 1ª visualização.
  createVizWindow();

  // Ctrl+Enter foca/mostra a janela (atalho global).
  globalShortcut.register('Control+Return', () => {
    if (!mainWindow) return createWindow();
    if (mainWindow.isVisible()) mainWindow.focus();
    else mainWindow.show();
  });

  // Ctrl+Shift+V alterna a tela de visualizações manualmente.
  globalShortcut.register('Control+Shift+V', () => {
    if (vizWindow && vizWindow.isVisible()) vizWindow.hide();
    else showVizWindow();
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  // No Linux mantemos o processo vivo apenas se quisermos re-abrir via atalho;
  // por ora encerramos junto com a janela.
  if (process.platform !== 'darwin') app.quit();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  stopBackend();
});

process.on('exit', stopBackend);

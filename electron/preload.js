'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// A porta do backend é injetada pelo processo principal via additionalArguments.
const portArg = process.argv.find((a) => a.startsWith('--jarvis-port='));
const port = portArg ? Number(portArg.split('=')[1]) : 8765;

contextBridge.exposeInMainWorld('jarvis', {
  backendUrl: `ws://127.0.0.1:${port}`,
  port,
  // Controles de janela (sem expor ipcRenderer cru ao renderer).
  hideWindow: () => ipcRenderer.send('window:hide'),
  quit: () => ipcRenderer.send('window:quit'),
  // Modo voz ocupa o monitor inteiro; texto volta ao card flutuante.
  setVoiceMode: (on) => ipcRenderer.send('window:voice-mode', !!on),
  // Tela de visualizações (2ª janela): mostrar/ocultar sob demanda.
  showViz: () => ipcRenderer.send('viz:show'),
  hideViz: () => ipcRenderer.send('viz:hide'),
  // Fonte de uma biblioteca de visualização (lida no processo main, que tem Node)
  // para injetar inline no iframe isolado. Async (IPC); o preload fica sandbox-safe.
  libSource: (nome) => ipcRenderer.invoke('viz:lib', nome),
});

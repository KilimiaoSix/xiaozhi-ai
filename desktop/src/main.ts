import { app, BrowserWindow, ipcMain, shell } from 'electron';
import path from 'node:path';

import type { AgentHooksRuntime } from './modules/features/coding-agent-status/agent-hooks/runtime';
import { registerAgentHooksIpc } from './main/agentHooksIpc';
import { registerCameraIpc } from './main/camera/registerCameraIpc';
import { createAgentHooksRuntime } from './main/createAgentHooksRuntime';
import { registerPomodoroIpc } from './main/pomodoro/registerPomodoroIpc';

let agentHooksRuntime: AgentHooksRuntime | undefined;
let cleanupAgentHooksIpc: (() => void) | undefined;
let cleanupPomodoroIpc: (() => void) | undefined;

const createWindow = (): void => {
  const mainWindow = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 1080,
    minHeight: 720,
    backgroundColor: '#081019',
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 18, y: 18 },
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: 'deny' };
  });

  if (MAIN_WINDOW_VITE_DEV_SERVER_URL) {
    void mainWindow.loadURL(MAIN_WINDOW_VITE_DEV_SERVER_URL);
  } else {
    void mainWindow.loadFile(
      path.join(__dirname, `../renderer/${MAIN_WINDOW_VITE_NAME}/index.html`),
    );
  }
};

app.whenReady().then(() => {
  agentHooksRuntime = createAgentHooksRuntime({
    homeDir: app.getPath('home'),
    userDataPath: app.getPath('userData'),
    electronPath: process.execPath,
  });
  cleanupAgentHooksIpc = registerAgentHooksIpc({
    ipcMain,
    runtime: agentHooksRuntime,
    getWindows: () => BrowserWindow.getAllWindows(),
  });
  void agentHooksRuntime.start().catch((error: unknown) => {
    console.error('Agent Hook 监控启动失败', error);
  });
  registerCameraIpc();
  cleanupPomodoroIpc = registerPomodoroIpc();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('before-quit', () => {
  cleanupAgentHooksIpc?.();
  cleanupAgentHooksIpc = undefined;
  cleanupPomodoroIpc?.();
  cleanupPomodoroIpc = undefined;
  void agentHooksRuntime?.stop();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

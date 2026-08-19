import { app, BrowserWindow, ipcMain, shell, systemPreferences } from 'electron';
import path from 'node:path';

import type { AgentHooksRuntime } from './modules/features/coding-agent-status/agent-hooks/runtime';
import { FeishuHttpClient } from './modules/features/feishu-briefing/feishuHttpClient';
import { registerAgentHooksIpc } from './main/agentHooksIpc';
import { registerCameraIpc } from './main/camera/registerCameraIpc';
import { registerMonitoringWindowGuard } from './main/camera/monitoringWindowGuard';
import { createAgentHooksRuntime } from './main/createAgentHooksRuntime';
import { registerFeishuIpc } from './main/feishuIpc';
import { PomodoroHttpClient } from './main/pomodoro/pomodoroHttpClient';
import { registerPomodoroIpc } from './main/pomodoro/registerPomodoroIpc';
import { registerWellbeingIpc } from './main/wellbeing/wellbeingIpc';
import { WellbeingTestService } from './main/wellbeing/wellbeingTestService';

let agentHooksRuntime: AgentHooksRuntime | undefined;
let cleanupAgentHooksIpc: (() => void) | undefined;
let cleanupFeishuIpc: (() => void) | undefined;
let cleanupPomodoroIpc: (() => void) | undefined;
let cleanupWellbeingIpc: (() => void) | undefined;
let cameraIpc: ReturnType<typeof registerCameraIpc> | undefined;
let isQuitting = false;

const createWindow = (): void => {
  const mainWindow = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 1080,
    minHeight: 720,
    backgroundColor: '#f3f4f6',
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 18, y: 18 },
    vibrancy: 'sidebar',
    visualEffectState: 'active',
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
  registerMonitoringWindowGuard({
    window: mainWindow,
    isMonitoringActive: () => cameraIpc?.isMonitoringActive() ?? false,
    isQuitting: () => isQuitting,
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
  // macOS 不会因为渲染进程调 getUserMedia 就弹摄像头授权框——必须主进程
  // 显式请求。漏掉这步的症状极具迷惑性:getUserMedia 正常返回 live 轨道,
  // 但永远没有一帧画面(预览全黑、readyState 停在 0),没有任何报错。
  if (process.platform === 'darwin') {
    const cameraAccess = systemPreferences.getMediaAccessStatus('camera');
    if (cameraAccess !== 'granted') {
      void systemPreferences.askForMediaAccess('camera').then((granted) => {
        console.warn(`摄像头授权请求结果: ${granted ? '已授权' : '被拒绝'}`);
      });
    }
  }
  agentHooksRuntime = createAgentHooksRuntime({
    homeDir: app.getPath('home'),
    userDataPath: app.getPath('userData'),
    electronPath: process.execPath,
    isAccessibilityTrusted: () =>
      systemPreferences.isTrustedAccessibilityClient(true),
  });
  cleanupAgentHooksIpc = registerAgentHooksIpc({
    ipcMain,
    runtime: agentHooksRuntime,
    getWindows: () => BrowserWindow.getAllWindows(),
  });
  cleanupFeishuIpc = registerFeishuIpc({
    ipcMain,
    client: new FeishuHttpClient(
      fetch,
      process.env.DESKPET_SERVER ?? 'http://127.0.0.1:8003',
      process.env.DESKPET_SERVER_AUTH_TOKEN ?? '',
    ),
  });
  void agentHooksRuntime.start().catch((error: unknown) => {
    console.error('Agent Hook 监控启动失败', error);
  });
  cameraIpc = registerCameraIpc();
  cleanupPomodoroIpc = registerPomodoroIpc({
    ipcMain,
    client: new PomodoroHttpClient(),
  });
  cleanupWellbeingIpc = registerWellbeingIpc({
    ipcMain,
    service: new WellbeingTestService(),
  });
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('before-quit', () => {
  isQuitting = true;
  cameraIpc?.cleanup();
  cameraIpc = undefined;
  cleanupAgentHooksIpc?.();
  cleanupAgentHooksIpc = undefined;
  cleanupFeishuIpc?.();
  cleanupFeishuIpc = undefined;
  cleanupPomodoroIpc?.();
  cleanupPomodoroIpc = undefined;
  cleanupWellbeingIpc?.();
  cleanupWellbeingIpc = undefined;
  void agentHooksRuntime?.stop();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

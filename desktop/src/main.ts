import { app, BrowserWindow, ipcMain, shell, systemPreferences } from 'electron';
import path from 'node:path';

import type { AgentHooksRuntime } from './modules/features/coding-agent-status/agent-hooks/runtime';
import { FeishuHttpClient } from './modules/features/feishu-briefing/feishuHttpClient';
import { registerAgentHooksIpc } from './main/agentHooksIpc';
import { AwaySummaryHttpClient } from './main/away/awaySummaryHttpClient';
import { registerAwaySummaryIpc } from './main/away/registerAwaySummaryIpc';
import { registerCameraIpc } from './main/camera/registerCameraIpc';
import { registerMonitoringWindowGuard } from './main/camera/monitoringWindowGuard';
import { AppConfigStore } from './main/config/appConfigStore';
import { registerConfigIpc } from './main/config/registerConfigIpc';
import { createAgentHooksRuntime } from './main/createAgentHooksRuntime';
import { registerFeishuIpc } from './main/feishuIpc';
import { IncidentHttpClient } from './main/incident/incidentHttpClient';
import { registerIncidentIpc } from './main/incident/registerIncidentIpc';
import { PomodoroHttpClient } from './main/pomodoro/pomodoroHttpClient';
import { registerPomodoroIpc } from './main/pomodoro/registerPomodoroIpc';
import { registerWellbeingIpc } from './main/wellbeing/wellbeingIpc';
import { WellbeingTestService } from './main/wellbeing/wellbeingTestService';

let agentHooksRuntime: AgentHooksRuntime | undefined;
let cleanupAgentHooksIpc: (() => void) | undefined;
let cleanupAwaySummaryIpc: (() => void) | undefined;
let cleanupConfigIpc: (() => void) | undefined;
let cleanupFeishuIpc: (() => void) | undefined;
let cleanupPomodoroIpc: (() => void) | undefined;
let cleanupIncidentIpc: (() => void) | undefined;
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

app.whenReady().then(async () => {
  // macOS 不会因为渲染进程调 getUserMedia 就弹摄像头授权框——必须主进程
  // 显式请求。漏掉这步的症状极具迷惑性:getUserMedia 正常返回 live 轨道,
  // 但永远没有一帧画面(预览全黑、readyState 停在 0),没有任何报错。
  if (process.platform === 'darwin') {
    const cameraAccess = systemPreferences.getMediaAccessStatus('camera');
    if (cameraAccess !== 'granted') {
      void systemPreferences.askForMediaAccess('camera').then((granted) => {
        if (granted) {
          console.warn('摄像头授权请求结果: 已授权');
          return;
        }
        console.warn(
          '摄像头授权请求结果: 被拒绝。'
          + ' dev 直启(npm run dev)时摄像头权限记在终端宿主/com.github.Electron 身份下,'
          + '一旦这条身份链被拒绝过就会永久静默、不再弹窗。'
          + ' 排查: 1) 终端执行 `tccutil reset Camera com.github.Electron`;'
          + ' 2) 重新触发摄像头功能,这次给终端宿主本身弹出的摄像头权限授权后重试;'
          + ' 3) 仍要验证摄像头链路就改用打包版(`npm run package` 后 open 生成的 .app)。',
        );
      });
    }
  }
  // 配置中心先于所有链路建立：地址、设备号、令牌全都从这里按次取值，
  // 打包应用经 open 启动时环境变量不可靠，配置文件那一层才是可依赖的事实源。
  const configStore = new AppConfigStore({
    filePath: path.join(app.getPath('userData'), 'config.json'),
  });
  await configStore.load();
  const resolveServerUrl = () => configStore.get().serverUrl;
  cleanupConfigIpc = registerConfigIpc({ ipcMain, store: configStore });

  agentHooksRuntime = createAgentHooksRuntime({
    homeDir: app.getPath('home'),
    userDataPath: app.getPath('userData'),
    electronPath: process.execPath,
    config: configStore,
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
      resolveServerUrl,
      () => configStore.get().authToken,
    ),
  });
  void agentHooksRuntime.start().catch((error: unknown) => {
    console.error('Agent Hook 监控启动失败', error);
  });
  cameraIpc = registerCameraIpc({ config: configStore });
  cleanupPomodoroIpc = registerPomodoroIpc({
    ipcMain,
    client: new PomodoroHttpClient(fetch, resolveServerUrl),
  });
  cleanupIncidentIpc = registerIncidentIpc({
    ipcMain,
    client: new IncidentHttpClient(fetch, resolveServerUrl),
  });
  cleanupAwaySummaryIpc = registerAwaySummaryIpc({
    ipcMain,
    client: new AwaySummaryHttpClient(
      fetch,
      resolveServerUrl,
      () => configStore.get().authToken,
    ),
  });
  cleanupWellbeingIpc = registerWellbeingIpc({
    ipcMain,
    service: new WellbeingTestService(fetch, resolveServerUrl),
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
  cleanupConfigIpc?.();
  cleanupConfigIpc = undefined;
  cleanupFeishuIpc?.();
  cleanupFeishuIpc = undefined;
  cleanupPomodoroIpc?.();
  cleanupPomodoroIpc = undefined;
  cleanupIncidentIpc?.();
  cleanupIncidentIpc = undefined;
  cleanupAwaySummaryIpc?.();
  cleanupAwaySummaryIpc = undefined;
  cleanupWellbeingIpc?.();
  cleanupWellbeingIpc = undefined;
  void agentHooksRuntime?.stop();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

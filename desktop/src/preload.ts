import { contextBridge, ipcRenderer } from 'electron';

import { AGENT_HOOKS_CHANNELS, type IpcResult } from './main/agentHooksIpc';
import { FEISHU_CHANNELS } from './main/feishuIpc';
import type { AgentSource } from './modules/features/coding-agent-status/agent-hooks/contracts';
import type { AgentHookDetection, AgentHookInstallResult } from './modules/features/coding-agent-status/agent-hooks/install/types';
import type { AgentHooksSnapshot } from './modules/features/coding-agent-status/agent-hooks/runtime';
import type {
  FeishuBriefingSnapshot,
  FeishuCliStatus,
} from './modules/features/feishu-briefing/contracts';
import type { XiaofeiDesktopApi } from './shared/contracts';
import { CAMERA_STREAM_CHANNELS } from './main/camera/cameraStreamIpc';

const invoke = async <T>(channel: string, ...args: unknown[]): Promise<T> => {
  const result = await ipcRenderer.invoke(channel, ...args) as IpcResult<T>;
  if (!result.ok) throw new Error(result.error);
  return result.value;
};

const desktopApi: XiaofeiDesktopApi = {
  getRuntimeInfo: () => ({
    platform: process.platform,
    versions: {
      chrome: process.versions.chrome,
      electron: process.versions.electron,
      node: process.versions.node,
    },
  }),
  agentHooks: {
    detect: () => invoke<AgentHookDetection[]>(AGENT_HOOKS_CHANNELS.detect),
    install: (source: AgentSource) =>
      invoke<AgentHookInstallResult>(AGENT_HOOKS_CHANNELS.install, source),
    installAll: () =>
      invoke<AgentHookInstallResult[]>(AGENT_HOOKS_CHANNELS.installAll),
    uninstall: (source: AgentSource) =>
      invoke<AgentHookInstallResult>(AGENT_HOOKS_CHANNELS.uninstall, source),
    getSnapshot: () => invoke<AgentHooksSnapshot>(AGENT_HOOKS_CHANNELS.snapshot),
    onSnapshot: (listener: (snapshot: AgentHooksSnapshot) => void) => {
      const handler = (_event: Electron.IpcRendererEvent, snapshot: AgentHooksSnapshot) => {
        listener(snapshot);
      };
      ipcRenderer.on(AGENT_HOOKS_CHANNELS.snapshotChanged, handler);
      return () => { ipcRenderer.removeListener(AGENT_HOOKS_CHANNELS.snapshotChanged, handler); };
    },
  },
  camera: {
    getPermissionStatus: () => ipcRenderer.invoke('camera:get-permission'),
    requestPermission: () => ipcRenderer.invoke('camera:request-permission'),
    openPrivacySettings: () => ipcRenderer.invoke('camera:open-privacy-settings'),
    recognition: {
      start: (options) => ipcRenderer.invoke(CAMERA_STREAM_CHANNELS.start, options),
      sendFrame: (jpeg) => ipcRenderer.invoke(CAMERA_STREAM_CHANNELS.frame, jpeg),
      stop: () => ipcRenderer.invoke(CAMERA_STREAM_CHANNELS.stop),
      onEvent: (listener) => {
        const handler = (
          _event: Electron.IpcRendererEvent,
          payload: Parameters<typeof listener>[0],
        ) => listener(payload);
        ipcRenderer.on(CAMERA_STREAM_CHANNELS.event, handler);
        return () => ipcRenderer.removeListener(CAMERA_STREAM_CHANNELS.event, handler);
      },
    },
  },
  feishu: {
    getStatus: () => invoke<FeishuCliStatus>(FEISHU_CHANNELS.status),
    getBriefing: () => invoke<FeishuBriefingSnapshot>(FEISHU_CHANNELS.briefing),
  },
};

contextBridge.exposeInMainWorld('xiaofei', desktopApi);

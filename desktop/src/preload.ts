import { contextBridge, ipcRenderer } from 'electron';

import { AGENT_HOOKS_CHANNELS, type IpcResult } from './main/agentHooksIpc';
import { FEISHU_CHANNELS } from './main/feishuIpc';
import type { AgentSource } from './modules/features/coding-agent-status/agent-hooks/contracts';
import type { AgentHookDetection, AgentHookInstallResult } from './modules/features/coding-agent-status/agent-hooks/install/types';
import type { AgentHooksSnapshot } from './modules/features/coding-agent-status/agent-hooks/runtime';
import type {
  FeishuBriefingSnapshot,
  FeishuConnectionStatus,
} from './modules/features/feishu-briefing/contracts';
import type {
  PomodoroCommandInput,
  PomodoroDeviceListResult,
  PomodoroIpcResult,
  PomodoroStatus,
} from './modules/features/focus-mode/types';
import type {
  IncidentAckResult,
  IncidentDiagnoseResult,
  IncidentIpcResult,
  IncidentListResult,
} from './modules/features/incident-assistant/types';
import type { XiaofeiDesktopApi } from './shared/contracts';
import { CAMERA_STREAM_CHANNELS } from './main/camera/cameraStreamIpc';
import { INCIDENT_CHANNELS } from './main/incident/registerIncidentIpc';
import { POMODORO_CHANNELS } from './main/pomodoro/registerPomodoroIpc';
import { WELLBEING_CHANNELS } from './main/wellbeing/wellbeingIpc';
import type { WellbeingTestKind } from './modules/features/wellbeing/contracts';

const invoke = async <T>(channel: string, ...args: unknown[]): Promise<T> => {
  const result = await ipcRenderer.invoke(channel, ...args) as IpcResult<T>;
  if (!result.ok) throw new Error(result.error);
  return result.value;
};

// 番茄钟不能复用上面的 invoke：它把失败信封拍成 new Error(message)，而 contextBridge
// 只搬 Error 的 message，code/status 到不了 renderer。信封原样交出去，
// 由 pomodoroDesktopGateway 还原成 PomodoroGatewayError。
const invokeEnvelope = async <T>(
  channel: string,
  ...args: unknown[]
): Promise<PomodoroIpcResult<T>> => (
  await ipcRenderer.invoke(channel, ...args) as PomodoroIpcResult<T>
);

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
    getStatus: () => invoke<FeishuConnectionStatus>(FEISHU_CHANNELS.status),
    getBriefing: () => invoke<FeishuBriefingSnapshot>(FEISHU_CHANNELS.briefing),
  },
  pomodoro: {
    listDevices: () =>
      invokeEnvelope<PomodoroDeviceListResult>(POMODORO_CHANNELS.listDevices),
    getStatus: (deviceId: string) =>
      invokeEnvelope<PomodoroStatus>(POMODORO_CHANNELS.getStatus, deviceId),
    sendCommand: (input: PomodoroCommandInput) =>
      invokeEnvelope<PomodoroStatus>(POMODORO_CHANNELS.sendCommand, input),
  },
  // 告警管理与番茄钟同理走信封：IncidentGatewayError 的 code/status 必须以
  // 数据形式跨 contextBridge，由 incidentDesktopGateway 还原
  incident: {
    list: () => (
      ipcRenderer.invoke(INCIDENT_CHANNELS.list) as
        Promise<IncidentIpcResult<IncidentListResult>>
    ),
    ack: (incidentId: string) => (
      ipcRenderer.invoke(INCIDENT_CHANNELS.ack, incidentId) as
        Promise<IncidentIpcResult<IncidentAckResult>>
    ),
    diagnose: (incidentId: string) => (
      ipcRenderer.invoke(INCIDENT_CHANNELS.diagnose, incidentId) as
        Promise<IncidentIpcResult<IncidentDiagnoseResult>>
    ),
  },
  wellbeing: {
    sendTest: (kind: WellbeingTestKind) =>
      invoke<{ deviceId: string }>(WELLBEING_CHANNELS.testEvent, kind),
  },
};

contextBridge.exposeInMainWorld('xiaofei', desktopApi);

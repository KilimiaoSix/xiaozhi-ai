import type { AgentSource } from '../modules/features/coding-agent-status/agent-hooks/contracts';
import type { AgentHookDetection, AgentHookInstallResult } from '../modules/features/coding-agent-status/agent-hooks/install/types';
import type { AgentHooksSnapshot } from '../modules/features/coding-agent-status/agent-hooks/runtime';
import type {
  FeishuBriefingSnapshot,
  FeishuConnectionStatus,
} from '../modules/features/feishu-briefing/contracts';
import type {
  CameraPermissionStatus,
  FrameSendResult,
  RecognitionEvent,
  RecognitionStreamOptions,
} from '../modules/features/camera-capture/types';
import type {
  PomodoroCommandInput,
  PomodoroDeviceListResult,
  PomodoroIpcResult,
  PomodoroStatus,
} from '../modules/features/focus-mode/types';
import type { WellbeingTestKind } from '../modules/features/wellbeing/contracts';

export interface RuntimeInfo {
  platform: NodeJS.Platform;
  versions: {
    chrome: string;
    electron: string;
    node: string;
  };
}

export interface AgentHooksDesktopApi {
  detect: () => Promise<AgentHookDetection[]>;
  install: (source: AgentSource) => Promise<AgentHookInstallResult>;
  installAll: () => Promise<AgentHookInstallResult[]>;
  uninstall: (source: AgentSource) => Promise<AgentHookInstallResult>;
  getSnapshot: () => Promise<AgentHooksSnapshot>;
  onSnapshot: (listener: (snapshot: AgentHooksSnapshot) => void) => () => void;
}

// 番茄钟走信封而不是裸值：错误的 code/status 必须以数据形式跨 contextBridge，
// 见 PomodoroIpcResult 的说明。拆信封的活儿在 pomodoroDesktopGateway。
export interface PomodoroDesktopApi {
  listDevices: () => Promise<PomodoroIpcResult<PomodoroDeviceListResult>>;
  getStatus: (deviceId: string) => Promise<PomodoroIpcResult<PomodoroStatus>>;
  sendCommand: (input: PomodoroCommandInput) => Promise<PomodoroIpcResult<PomodoroStatus>>;
}

export interface FeishuDesktopApi {
  getStatus: () => Promise<FeishuConnectionStatus>;
  getBriefing: () => Promise<FeishuBriefingSnapshot>;
}

export interface CameraRecognitionDesktopApi {
  start: (options: RecognitionStreamOptions) => Promise<void>;
  sendFrame: (jpeg: ArrayBuffer) => Promise<FrameSendResult>;
  stop: () => Promise<void>;
  onEvent: (listener: (event: RecognitionEvent) => void) => () => void;
}

export interface CameraDesktopApi {
  getPermissionStatus: () => Promise<CameraPermissionStatus>;
  requestPermission: () => Promise<CameraPermissionStatus>;
  openPrivacySettings: () => Promise<void>;
  recognition: CameraRecognitionDesktopApi;
}

export interface WellbeingDesktopApi {
  sendTest: (kind: WellbeingTestKind) => Promise<{ deviceId: string }>;
}

export interface XiaofeiDesktopApi {
  getRuntimeInfo: () => RuntimeInfo;
  agentHooks: AgentHooksDesktopApi;
  camera: CameraDesktopApi;
  feishu: FeishuDesktopApi;
  pomodoro: PomodoroDesktopApi;
  wellbeing: WellbeingDesktopApi;
}

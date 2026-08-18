import type { AgentSource } from '../modules/features/coding-agent-status/agent-hooks/contracts';
import type { AgentHookDetection, AgentHookInstallResult } from '../modules/features/coding-agent-status/agent-hooks/install/types';
import type { AgentHooksSnapshot } from '../modules/features/coding-agent-status/agent-hooks/runtime';
import type {
  FeishuBriefingSnapshot,
  FeishuCliStatus,
} from '../modules/features/feishu-briefing/contracts';
import type {
  CameraPermissionStatus,
  FrameSendResult,
  RecognitionEvent,
  RecognitionStreamOptions,
} from '../modules/features/camera-capture/types';

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

export interface FeishuDesktopApi {
  getStatus: () => Promise<FeishuCliStatus>;
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

export interface XiaofeiDesktopApi {
  getRuntimeInfo: () => RuntimeInfo;
  agentHooks: AgentHooksDesktopApi;
  camera: CameraDesktopApi;
  feishu: FeishuDesktopApi;
}

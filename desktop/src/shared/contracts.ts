import type { AgentSource } from '../modules/features/coding-agent-status/agent-hooks/contracts';
import type { AgentHookDetection, AgentHookInstallResult } from '../modules/features/coding-agent-status/agent-hooks/install/types';
import type { AgentHooksSnapshot } from '../modules/features/coding-agent-status/agent-hooks/runtime';

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

export interface XiaofeiDesktopApi {
  getRuntimeInfo: () => RuntimeInfo;
  agentHooks: AgentHooksDesktopApi;
  getCameraPermissionStatus: () => Promise<CameraPermissionStatus>;
  requestCameraPermission: () => Promise<CameraPermissionStatus>;
  openCameraPrivacySettings: () => Promise<void>;
  enrollOwnerFace: (input: OwnerEnrollmentInput) => Promise<OwnerEnrollmentResult>;
  uploadMonitoringFrame: (
    input: MonitoringFrameInput,
  ) => Promise<MonitoringFrameResult>;
}
import type {
  CameraPermissionStatus,
  MonitoringFrameInput,
  MonitoringFrameResult,
  OwnerEnrollmentInput,
  OwnerEnrollmentResult,
} from '../modules/features/camera-capture/types';

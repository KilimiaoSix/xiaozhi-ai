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
import type {
  AwayIpcResult,
  AwaySummaryResult,
} from '../modules/features/away-summary/types';
import type {
  IncidentAckResult,
  IncidentDiagnoseResult,
  IncidentIpcResult,
  IncidentListResult,
} from '../modules/features/incident-assistant/types';
import type { WellbeingTestKind } from '../modules/features/wellbeing/contracts';
import type { AppConfig, AppConfigResolution } from './appConfig';

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

// 告警管理与番茄钟同理走信封：错误的 code/status 必须以数据形式跨 contextBridge。
// 拆信封的活儿在 incidentDesktopGateway。
export interface IncidentDesktopApi {
  list: () => Promise<IncidentIpcResult<IncidentListResult>>;
  ack: (incidentId: string) => Promise<IncidentIpcResult<IncidentAckResult>>;
  diagnose: (incidentId: string) => Promise<IncidentIpcResult<IncidentDiagnoseResult>>;
}

// 返岗汇总同样走信封（AwayGatewayError 的 code/status 必须以数据形式跨 contextBridge）。
// 这条链路只读：没有清账动作，清账在 Server 侧真的播报之后完成。
export interface AwayDesktopApi {
  getSummary: () => Promise<AwayIpcResult<AwaySummaryResult>>;
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

// 配置中心：读回的是每字段的生效值与来源（env / 文件 / 默认值），
// 写入的是「配置文件」那一层——env 覆盖期间写了也不会立刻生效，界面据此提示。
export interface ConfigDesktopApi {
  get: () => Promise<AppConfigResolution>;
  update: (patch: Partial<AppConfig>) => Promise<AppConfigResolution>;
}

export interface XiaofeiDesktopApi {
  getRuntimeInfo: () => RuntimeInfo;
  agentHooks: AgentHooksDesktopApi;
  away: AwayDesktopApi;
  camera: CameraDesktopApi;
  config: ConfigDesktopApi;
  feishu: FeishuDesktopApi;
  pomodoro: PomodoroDesktopApi;
  incident: IncidentDesktopApi;
  wellbeing: WellbeingDesktopApi;
}

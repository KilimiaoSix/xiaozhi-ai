/**
 * 告警管理列表的桌面端契约（对应 GET /xiaozhi/incident/list 等三个接口）。
 *
 * 服务端把两条告警链路（incident webhook 与 alert_relay 值班中继）归一成同一份
 * 字段；桌面端只做展示与操作，不做任何 LLM 判断。alert_relay 来源的条目
 * ack/diagnose 都会被服务端 400 拒绝——按钮必须在渲染层就置灰。
 */

export type IncidentSource = 'incident' | 'alert_relay';

export type IncidentSeverity = 'P0' | 'P1' | 'P2' | 'P3';

export type IncidentState = 'firing' | 'observing' | 'recovered';

/** idle 只在语义上存在（从未诊断过 = diagnosis 为 null），服务端不会下发字面量 idle */
export type IncidentDiagnosisState = 'idle' | 'running' | 'done' | 'failed';

export interface IncidentDiagnosis {
  state: IncidentDiagnosisState;
  /** done 时是结论，failed 时是失败原因 */
  summary: string;
  finishedAt: string | null;
}

export interface IncidentTimelineEntry {
  at: string;
  event: string;
  detail: string;
}

export interface IncidentSummary {
  id: string;
  source: IncidentSource;
  service: string;
  severity: IncidentSeverity;
  title: string;
  message: string;
  state: IncidentState;
  repeatCount: number;
  firstSeenAt: string | null;
  lastSeenAt: string | null;
  recoveredAt: string | null;
  announced: boolean;
  acknowledged: boolean;
  simulated: boolean;
  diagnosis: IncidentDiagnosis | null;
  timeline: IncidentTimelineEntry[];
}

export interface IncidentListResult {
  date: string;
  incidents: IncidentSummary[];
}

export interface IncidentAckResult {
  acknowledged: boolean;
}

export interface IncidentDiagnoseResult {
  /**
   * true = 本次请求被受理；false = 服务端已有同一故障的诊断在跑（HTTP 409 归一）。
   * 两种情况桌面端都进入「诊断中」态——409 不是错误，是并发闸门在工作。
   */
  accepted: boolean;
}

export type IncidentGatewayErrorCode =
  | 'http-error'
  | 'invalid-response'
  | 'offline'
  | 'timeout'
  // IPC 层兜底：main 侧捕到的不一定是 IncidentGatewayError，也不能替它假装成某个已知原因
  | 'unknown';

export class IncidentGatewayError extends Error {
  constructor(
    public readonly code: IncidentGatewayErrorCode,
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'IncidentGatewayError';
  }
}

/**
 * IPC 信封：失败必须以数据形式跨 contextBridge（Electron 只搬 Error 的 message，
 * code/status 会被剥掉），由 incidentDesktopGateway 还原成 IncidentGatewayError。
 * 与番茄钟的 PomodoroIpcResult 同构。
 */
export type IncidentIpcResult<T> =
  | { ok: true; value: T }
  | { ok: false; code: IncidentGatewayErrorCode; message: string; status?: number };

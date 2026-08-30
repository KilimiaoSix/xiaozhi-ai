/**
 * 返岗汇总的桌面端契约（对应 `GET /xiaozhi/away/summary`）。
 *
 * 机器人只用三句话把返岗汇总念完（TTS 记不住更多），详细清单要有个地方能看全，
 * 这个面板就是那份全量。服务端接口**只读、不清账**——清账由在岗编排在真的
 * 播报出去之后做（server/core/presence_arrival.py），所以桌面端只有 GET，
 * 刷新页面绝不会把主人还没听到的留言标成已播报。
 */

/** 与服务端 away_ledger 的 kind 枚举对齐；服务端加了新类型也不该丢，故不收窄成联合类型。 */
export const AWAY_EVENT_KINDS = {
  agentCompleted: 'agent_completed',
  agentFailed: 'agent_failed',
  agentNeedsUser: 'agent_needs_user',
  visitorMessage: 'visitor_message',
  incident: 'incident',
  generic: 'generic',
} as const;

export const AWAY_SEVERITY_CRITICAL = 'critical';

export interface AwayEventItem {
  /** 服务端 away_ledger 的事件类型，未知值一律进「普通消息」桶 */
  kind: string;
  text: string;
  /** 同一 Agent 任务的多次状态变化在服务端已按它折叠，这里只是展示与去重键 */
  taskKey: string;
  severity: string;
  /** 返岗汇总里的来源名（Codex / Claude Code / …），可能为空 */
  source: string;
  at: string;
  /** 同一条严重告警刷屏时的合并次数，普通事项恒为 1 */
  count: number;
}

export interface AwaySummaryResult {
  /** 主人当前是否仍在离席窗口内 */
  away: boolean;
  awaySince: string | null;
  awayMinutes: number;
  /** 服务端给的待播报条数，与 items.length 同源，展示时以它为准 */
  count: number;
  /** 服务端组好的三句话播报稿；没有事项时为 null */
  speech: string | null;
  items: AwayEventItem[];
}

export type AwayGatewayErrorCode =
  | 'http-error'
  | 'invalid-response'
  | 'offline'
  | 'timeout'
  // IPC 层兜底：main 侧捕到的不一定是 AwayGatewayError，也不能替它假装成某个已知原因
  | 'unknown';

export class AwayGatewayError extends Error {
  constructor(
    public readonly code: AwayGatewayErrorCode,
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'AwayGatewayError';
  }
}

/**
 * IPC 信封：失败必须以数据形式跨 contextBridge（Electron 只搬 Error 的 message，
 * code/status 会被剥掉），由 awaySummaryDesktopGateway 还原成 AwayGatewayError。
 * 与告警管理的 IncidentIpcResult 同构。
 */
export type AwayIpcResult<T> =
  | { ok: true; value: T }
  | { ok: false; code: AwayGatewayErrorCode; message: string; status?: number };

import { AgentRobotNotifier, RobotEventPushClient } from './agentRobotNotifier';
import type { RobotIntentNotifier } from '../../modules/features/coding-agent-status/agent-hooks/runtime';
import type {
  AgentTaskSnapshot,
  RobotActionIntent,
} from '../../modules/features/coding-agent-status/agent-hooks/contracts';

type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

/**
 * 从 Server 查询在线设备并唯一命中时返回其 device_id。
 *
 * 只在「恰好一台在线」时返回：多台设备时任选其一等于把播报发给陌生工位，
 * 宁可保持静默等用户在设置面板（或 DESKPET_DEVICE_ID）里显式指定设备号。
 */
export const discoverRobotDeviceId = async (
  fetcher: Fetcher,
  baseUrl: string,
): Promise<string | null> => {
  try {
    const response = await fetcher(`${baseUrl}/xiaozhi/event/devices`, {
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) return null;
    const payload = (await response.json()) as { devices?: unknown };
    const devices = Array.isArray(payload.devices) ? payload.devices : [];
    if (devices.length !== 1 || typeof devices[0] !== 'string') return null;
    return devices[0] || null;
  } catch {
    return null;
  }
};

export interface RobotDeviceDiscoveryOptions {
  fetcher?: Fetcher;
  /** 每轮探测都现取地址：配置中心里改完地址，下一轮就打到新 Server。 */
  resolveServerUrl: () => string;
  /** 发现成功后如何构造真正的通知器；测试用注入点。 */
  createNotifier?: (deviceId: string) => RobotIntentNotifier;
  onError?: (error: unknown) => void;
  /** 两次探测之间的间隔毫秒数。 */
  retryIntervalMs?: number;
  /** 最多探测多少次；桌面端可能比 Server 先启动，给足重试窗口。 */
  maxAttempts?: number;
  sleep?: (ms: number) => Promise<void>;
}

const DEFAULT_RETRY_INTERVAL_MS = 15_000;
const DEFAULT_MAX_ATTEMPTS = 120; // 15s × 120 = 半小时，覆盖"先开应用后开 Server"的整个早晨

/**
 * 配置中心里没有设备号时的兜底：后台轮询 Server 在线设备表，
 * 恰好一台在线就以它为目标构造通知器并从此把意图转发过去。
 *
 * 发现成功之前收到的意图直接丢弃——那只是应用启动瞬间的存量事件，
 * 补播一句几分钟前的"任务完成"反而像故障。
 */
export class DiscoveringRobotNotifier implements RobotIntentNotifier {
  private target?: RobotIntentNotifier;
  private stopped = false;
  /** 联调与测试用：发现流程是否已经收敛（成功或耗尽重试）。 */
  readonly settled: Promise<void>;

  constructor(private readonly options: RobotDeviceDiscoveryOptions) {
    this.settled = this.discover();
  }

  async notify(
    intents: RobotActionIntent[],
    tasks: AgentTaskSnapshot[],
  ): Promise<void> {
    await this.target?.notify(intents, tasks);
  }

  stop(): void {
    this.stopped = true;
  }

  private async discover(): Promise<void> {
    const fetcher = this.options.fetcher ?? fetch;
    const sleep = this.options.sleep
      ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
    const interval = this.options.retryIntervalMs ?? DEFAULT_RETRY_INTERVAL_MS;
    const maxAttempts = this.options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
    const createNotifier = this.options.createNotifier
      ?? ((deviceId: string) =>
        new AgentRobotNotifier(
          new RobotEventPushClient(
            fetcher,
            this.options.resolveServerUrl,
            this.options.onError,
          ),
          deviceId,
          { onError: this.options.onError },
        ));

    for (let attempt = 0; attempt < maxAttempts && !this.stopped; attempt += 1) {
      if (attempt > 0) await sleep(interval);
      if (this.stopped) return;
      const deviceId = await discoverRobotDeviceId(fetcher, this.options.resolveServerUrl());
      if (deviceId) {
        this.target = createNotifier(deviceId);
        return;
      }
    }
  }
}

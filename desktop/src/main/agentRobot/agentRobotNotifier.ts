import {
  mapIntentToPush,
  mapMergedIntentToPush,
  type RobotPushBody,
} from './robotIntentMapper';
import type {
  AgentTaskSnapshot,
  RobotActionIntent,
} from '../../modules/features/coding-agent-status/agent-hooks/contracts';

type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

export interface RobotPusher {
  push(body: RobotPushBody): Promise<boolean>;
}

/**
 * 把工作事件推给 Server 的 `/xiaozhi/event/push`，由 Server 经 WebSocket 下发给机器人。
 *
 * 任何失败都只返回 false，不抛出：机器人反馈是锦上添花，
 * 不能因为 Server 没起、设备不在线就把 Agent 事件处理链路带崩。
 * 这一点与 tools/cc-approval-hook.sh 的既有约定一致。
 */
export class RobotEventPushClient implements RobotPusher {
  constructor(
    private readonly fetcher: Fetcher,
    /** 地址按次向配置中心取，构造期不缓存。 */
    private readonly resolveBaseUrl: () => string,
    private readonly onError?: (error: unknown) => void,
  ) {}

  async push(body: RobotPushBody): Promise<boolean> {
    try {
      const response = await this.fetcher(`${this.resolveBaseUrl()}/xiaozhi/event/push`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(5000),
      });
      if (!response.ok) {
        // 404 = 设备没常连上来，属于常态，不当异常处理
        this.onError?.(new Error(`push failed with ${response.status}`));
        return false;
      }
      return true;
    } catch (error) {
      this.onError?.(error);
      return false;
    }
  }
}

/** 设备播一句话大致要十几秒，两条播报之间至少隔这么久才不会互相撞。 */
const DEFAULT_MIN_SPEAK_GAP_MS = 12_000;

export interface AgentRobotNotifierOptions {
  onError?: (error: unknown) => void;
  /** 两条「要出声」的推送之间的最小间隔 */
  minSpeakGapMs?: number;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

/**
 * 机器人意图的消费方：把 tracker 产出的意图翻成推送发出去。
 *
 * 真机联调发现设备一次只播一句，后到的播报会被服务端按忙态降级成提示音而白发。
 * 所以这里不是来一条发一条，而是排队后做三件事：
 *   1. 同一任务只保留最新状态——「开始→需要介入→完成」只该播最后那句；
 *   2. 同一窗口内落到同一终态的多个任务合并成一条；
 *   3. 两条播报之间强制隔开最小间隔，静音推送不占这个间隔。
 * 另外按意图自带的 expiresAt 丢弃过期项，符合 AGENTS.md「重连后不补播过期动作」。
 *
 * notify() 只负责入队并立刻返回，真正的发送在后台串行进行——它挂在事件处理链路上，
 * 阻塞十几秒会把 Agent 事件的处理也一起拖住。
 *
 * 未配置 device_id 时整体静默跳过：没有机器人的开发机上，Agent 监控照常工作。
 */
export class AgentRobotNotifier {
  private readonly onError?: (error: unknown) => void;
  private readonly minSpeakGapMs: number;
  private readonly now: () => number;
  private readonly sleep: (ms: number) => Promise<void>;

  private pending: RobotActionIntent[] = [];
  private readonly tasks = new Map<string, AgentTaskSnapshot>();
  private draining?: Promise<void>;
  private lastSpeakAt: number | null = null;

  /** 最近一次发送前实际等待的毫秒数，供联调与测试观察。 */
  lastSendGapMs = 0;

  constructor(
    private readonly client: RobotPusher,
    private readonly deviceId: string,
    options: AgentRobotNotifierOptions = {},
  ) {
    this.onError = options.onError;
    this.minSpeakGapMs = options.minSpeakGapMs ?? DEFAULT_MIN_SPEAK_GAP_MS;
    this.now = options.now ?? (() => Date.now());
    this.sleep = options.sleep
      ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
  }

  async notify(
    intents: RobotActionIntent[],
    tasks: AgentTaskSnapshot[],
  ): Promise<void> {
    if (!this.deviceId || intents.length === 0) return;
    for (const task of tasks) this.tasks.set(task.key, task);
    this.pending.push(...intents);
    this.startDraining();
  }

  /** 等待队列排空。生产代码不需要，联调与测试用。 */
  whenIdle(): Promise<void> {
    return this.draining ?? Promise.resolve();
  }

  private startDraining(): void {
    if (this.draining) return;
    this.draining = this.drain().finally(() => { this.draining = undefined; });
  }

  private async drain(): Promise<void> {
    while (this.pending.length > 0) {
      // 整批取走再合并：等待间隔期间新到的意图会并进下一轮，
      // 从而把「间隔期间攒下的多个完成」也合成一条。
      const batch = this.pending;
      this.pending = [];
      for (const push of this.buildPushes(batch)) {
        await this.send(push);
      }
    }
  }

  private buildPushes(batch: RobotActionIntent[]): RobotPushBody[] {
    // 同一任务只保留最新意图
    const latestByTask = new Map<string, RobotActionIntent>();
    for (const intent of batch) latestByTask.set(intent.taskKey, intent);

    const now = this.now();
    const alive = [...latestByTask.values()].filter((intent) => {
      const expiresAt = Date.parse(intent.expiresAt);
      return Number.isNaN(expiresAt) || expiresAt > now;
    });

    const byAction = new Map<RobotActionIntent['action'], RobotActionIntent[]>();
    for (const intent of alive) {
      const group = byAction.get(intent.action);
      if (group) group.push(intent);
      else byAction.set(intent.action, [intent]);
    }

    return [...byAction.entries()].map(([action, group]) =>
      group.length === 1
        ? mapIntentToPush(this.deviceId, group[0], this.tasks.get(group[0].taskKey))
        : mapMergedIntentToPush(
            this.deviceId,
            action,
            group.length,
            group.map((intent) => this.tasks.get(intent.taskKey)),
          ));
  }

  private async send(push: RobotPushBody): Promise<void> {
    let waited = 0;
    if (push.speak && this.lastSpeakAt !== null) {
      const remaining = this.minSpeakGapMs - (this.now() - this.lastSpeakAt);
      if (remaining > 0) {
        waited = remaining;
        await this.sleep(remaining);
      }
    }
    this.lastSendGapMs = waited;

    try {
      await this.client.push(push);
    } catch (error) {
      // 单条失败不影响后面的意图
      this.onError?.(error);
    }
    if (push.speak) this.lastSpeakAt = this.now();
  }
}

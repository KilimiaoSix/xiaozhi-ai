import type { RobotIntentNotifier } from '../../modules/features/coding-agent-status/agent-hooks/runtime';
import type {
  AgentTaskSnapshot,
  RobotActionIntent,
} from '../../modules/features/coding-agent-status/agent-hooks/contracts';

export interface ConfiguredRobotNotifierOptions {
  /** 每条事件现取设备号：设置面板改完，下一条就发到新设备。 */
  resolveDeviceId: () => string;
  createDirect: (deviceId: string) => RobotIntentNotifier;
  /** 没有设备号时的自动发现通知器；只会被创建一次。 */
  createDiscovering: () => RobotIntentNotifier;
}

/**
 * 把「设备号来自配置中心」这件事从 AgentRobotNotifier 里摘出来。
 *
 * AgentRobotNotifier 自己带播报间隔与合并队列，是有状态的，所以设备号没变
 * 就必须复用同一个实例——每条事件重建等于把 12 秒的播报间隔清零，
 * 两句播报会在真机上撞在一起。
 */
export class ConfiguredRobotNotifier implements RobotIntentNotifier {
  private direct?: { deviceId: string; notifier: RobotIntentNotifier };
  private discovering?: RobotIntentNotifier;

  constructor(private readonly options: ConfiguredRobotNotifierOptions) {}

  async notify(
    intents: RobotActionIntent[],
    tasks: AgentTaskSnapshot[],
  ): Promise<void> {
    await this.target().notify(intents, tasks);
  }

  private target(): RobotIntentNotifier {
    const deviceId = this.options.resolveDeviceId().trim();
    if (!deviceId) {
      this.discovering ??= this.options.createDiscovering();
      return this.discovering;
    }
    if (this.direct?.deviceId !== deviceId) {
      this.direct = { deviceId, notifier: this.options.createDirect(deviceId) };
    }
    return this.direct.notifier;
  }
}

import { discoverRobotDeviceId } from '../agentRobot/deviceDiscovery';
import type { WellbeingTestKind } from '../../modules/features/wellbeing/contracts';

type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

export interface WellbeingTestResult {
  deviceId: string;
}

const WELLBEING_TEST_EVENTS = {
  long_work: {
    text: '坐得有点久了，站起来伸伸腰，走两分钟再继续吧。',
    emotion: 'relaxed',
    status: '测试·休息一下',
    speak: true,
    silent: false,
    action: 'look_up',
    restore_after: 10,
    source: 'APP测试',
  },
  commute_safety: {
    text: '快下班啦，回去路上慢一点，注意安全。',
    emotion: 'loving',
    status: '测试·下班平安',
    speak: true,
    silent: false,
    action: 'nod',
    restore_after: 8,
    source: 'APP测试',
  },
  overtime: {
    text: '已经九点了，今天辛苦了。把手头这点收个尾，早点下班吧。',
    emotion: 'sleepy',
    status: '测试·早点下班',
    speak: true,
    silent: false,
    action: 'look_down',
    restore_after: 10,
    source: 'APP测试',
  },
  frantic_overtime: {
    text: '已经很晚了，工作先停在这里。现在就收拾回家，明天再继续。',
    emotion: 'shocked',
    status: '测试·该下班了',
    speak: true,
    silent: false,
    action: 'shake',
    restore_after: 12,
    source: 'APP测试',
  },
  warm_encouragement: {
    text: '认真工作的你很棒，给你一颗小爱心。',
    emotion: 'loving',
    status: '测试·给你加油',
    speak: true,
    silent: false,
    action: 'nod',
    restore_after: 7,
    source: 'APP测试',
  },
} as const;

/** 从桌面 APP 主动触发一次与首次久坐提醒一致的模拟事件。 */
export class WellbeingTestService {
  constructor(
    private readonly fetcher: Fetcher = fetch,
    private readonly baseUrl = 'http://127.0.0.1:8003',
  ) {}

  async sendTest(kind: WellbeingTestKind): Promise<WellbeingTestResult> {
    const deviceId = await discoverRobotDeviceId(this.fetcher, this.baseUrl);
    if (!deviceId) {
      throw new Error('需要恰好一台在线机器人才能发送测试提醒');
    }

    const response = await this.fetcher(`${this.baseUrl}/xiaozhi/event/push`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ device_id: deviceId, ...WELLBEING_TEST_EVENTS[kind] }),
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) throw new Error(`测试提醒发送失败（HTTP ${response.status}）`);
    const payload = await response.json() as { delivered?: unknown };
    if (payload.delivered !== true) throw new Error('机器人未接收测试提醒');
    return { deviceId };
  }

}

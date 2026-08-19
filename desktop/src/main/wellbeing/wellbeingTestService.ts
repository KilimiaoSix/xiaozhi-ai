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
    emotion: 'winking',
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
    private readonly baseUrl = process.env.DESKPET_SERVER ?? 'http://127.0.0.1:8003',
  ) {}

  async sendTest(kind: WellbeingTestKind): Promise<WellbeingTestResult> {
    // discoverRobotDeviceId 把「连不上」和「设备数不为 1」都吞成 null，
    // 两种排查方向完全不同，这里自己查一次以便分辨。
    let devices: unknown[];
    try {
      const listing = await this.fetcher(`${this.baseUrl}/xiaozhi/event/devices`, {
        signal: AbortSignal.timeout(5000),
      });
      if (!listing.ok) {
        throw new Error(`查询在线机器人失败（HTTP ${listing.status}）`);
      }
      const payload = (await listing.json()) as { devices?: unknown };
      devices = Array.isArray(payload.devices) ? payload.devices : [];
    } catch (error) {
      if (error instanceof DOMException && error.name === 'TimeoutError') {
        throw new Error('连接本地 Server 超时');
      }
      if (error instanceof Error && error.message.startsWith('查询在线机器人失败')) {
        throw error;
      }
      throw new Error('本地 Server 当前不可用');
    }
    if (devices.length !== 1 || typeof devices[0] !== 'string' || !devices[0]) {
      throw new Error(
        devices.length === 0
          ? '当前没有在线机器人，测试提醒无处可发'
          : `在线机器人有 ${devices.length} 台，需要恰好一台才能发送测试提醒`,
      );
    }
    const deviceId = devices[0];

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

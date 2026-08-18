import type { FeatureModule } from '../../core/types';
import { pomodoroDesktopGateway, type PomodoroDesktopGateway } from './services/pomodoroDesktopGateway';

const definition = {
  id: 'focus-mode',
  code: 'FOCUS',
  title: '专注模式与分心提醒',
  summary: '记录专注周期，在偏离任务时给出轻量提醒。',
  triggerLabel: '开始专注',
  status: 'ready' as const,
  tone: 'violet' as const,
};

export const createFocusModeModule = (
  gateway: PomodoroDesktopGateway = pomodoroDesktopGateway,
): FeatureModule => ({
  definition,
  async execute() {
    try {
      const { devices } = await gateway.listDevices();
      const target = devices.find((device) => device.connected);
      if (!target) {
        return {
          title: '未找到可用设备',
          detail: '没有已连接的小飞设备，无法开始番茄钟专注周期。',
          tone: 'coral',
          source: 'live',
        };
      }

      await gateway.sendCommand(target.deviceId, 'start');
      return {
        title: '专注周期已开始',
        detail: `已向设备 ${target.deviceId} 发送番茄钟开始指令。`,
        tone: 'violet',
        source: 'live',
      };
    } catch (error) {
      return {
        title: '专注周期启动失败',
        detail: error instanceof Error ? error.message : '番茄钟服务不可用。',
        tone: 'coral',
        source: 'live',
      };
    }
  },
});

export const focusModeModule = createFocusModeModule();

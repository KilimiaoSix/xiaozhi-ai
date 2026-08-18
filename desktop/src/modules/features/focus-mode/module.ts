import type { FeatureModule } from '../../core/types';

export const focusModeModule: FeatureModule = {
  definition: {
    id: 'focus-mode',
    code: 'FOCUS',
    title: '专注模式与分心提醒',
    summary: '记录专注周期，在偏离任务时给出轻量提醒。',
    triggerLabel: '开始专注',
    status: 'placeholder',
    tone: 'violet',
  },
  async execute(context) {
    const startTime = new Intl.DateTimeFormat('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(context.now());

    return {
      title: '专注周期已开始 · Mock',
      detail: `模拟周期开始于 ${startTime}，已预留活跃窗口和分心检测入口。`,
      tone: 'violet',
      source: 'mock',
    };
  },
};

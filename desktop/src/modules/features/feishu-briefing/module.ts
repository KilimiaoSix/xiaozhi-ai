import type { FeatureModule } from '../../core/types';

export const feishuBriefingModule: FeatureModule = {
  definition: {
    id: 'feishu-briefing',
    code: 'LARK',
    title: '飞书待办与日程简报',
    summary: '把今天最重要的会议、待办和时间冲突压缩成一分钟简报。',
    triggerLabel: '模拟简报',
    status: 'placeholder',
    tone: 'violet',
  },
  async execute() {
    return {
      title: '今日工作简报已生成 · Mock',
      detail: '已预留飞书日历、任务和简报生成适配入口。',
      tone: 'violet',
      source: 'mock',
    };
  },
};

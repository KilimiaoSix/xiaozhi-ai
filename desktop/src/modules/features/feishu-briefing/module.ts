import type { FeatureModule } from '../../core/types';

export const feishuBriefingModule: FeatureModule = {
  definition: {
    id: 'feishu-briefing',
    code: 'LARK',
    title: '飞书待办与日程简报',
    summary: '通过飞书 CLI 读取当前用户的今日日程与未完成任务。',
    triggerLabel: '查看飞书工作台',
    status: 'ready',
    tone: 'violet',
  },
  async execute() {
    return {
      title: '飞书 CLI 工作台已打开',
      detail: '可检查用户身份，并刷新今日日程和未完成任务。',
      tone: 'violet',
      source: 'live',
    };
  },
};

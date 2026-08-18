import type { FeatureModule } from '../../core/types';

export const awayMessagesModule: FeatureModule = {
  definition: {
    id: 'away-messages',
    code: 'AWAY',
    title: '离席状态与同事留言',
    summary: '识别离席并替你守住工位，收下访客的简短留言。',
    triggerLabel: '模拟离席',
    status: 'placeholder',
    tone: 'amber',
  },
  async execute() {
    return {
      title: '已进入守工位模式 · Mock',
      detail: '已预留离席感知、访客识别和同事留言采集入口。',
      tone: 'amber',
      source: 'mock',
    };
  },
};

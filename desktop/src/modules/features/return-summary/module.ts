import type { FeatureModule } from '../../core/types';

export const returnSummaryModule: FeatureModule = {
  definition: {
    id: 'return-summary',
    code: 'BACK',
    title: '返岗信息汇总',
    summary: '回来后一次讲清错过的留言、告警和 Agent 进展。',
    triggerLabel: '模拟返岗',
    status: 'placeholder',
    tone: 'cyan',
  },
  async execute() {
    return {
      title: '返岗摘要已整理 · Mock',
      detail: '已预留离席留言、Agent 状态和告警事件聚合入口。',
      tone: 'cyan',
      source: 'mock',
    };
  },
};

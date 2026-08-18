import type { FeatureModule } from '../../core/types';

export const incidentAssistantModule: FeatureModule = {
  definition: {
    id: 'incident-assistant',
    code: 'ALERT',
    title: '线上告警与诊断协助',
    summary: '聚合告警上下文，给出排查顺序并联动开发 Agent。',
    triggerLabel: '模拟告警',
    status: 'placeholder',
    tone: 'coral',
  },
  async execute() {
    return {
      title: '检测到线上告警 · Mock',
      detail: '已预留告警聚合、诊断建议和开发 Agent 联动入口，不执行自动修复。',
      tone: 'coral',
      source: 'mock',
    };
  },
};

import type { FeatureModule } from '../../core/types';

export const codingAgentStatusModule: FeatureModule = {
  definition: {
    id: 'coding-agent-status',
    code: 'CODE',
    title: 'Codex / Claude 状态提醒',
    summary: '追踪开发 Agent 的完成、等待确认和异常状态。',
    triggerLabel: '模拟完成',
    status: 'placeholder',
    tone: 'cyan',
  },
  async execute() {
    return {
      title: '编码任务已完成 · Mock',
      detail: '已预留 Codex、Claude Code 和 WorkBuddy 状态事件入口。',
      tone: 'cyan',
      source: 'mock',
    };
  },
};

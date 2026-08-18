import type { FeatureModule } from '../../core/types';

export const codingAgentStatusModule: FeatureModule = {
  definition: {
    id: 'coding-agent-status',
    code: 'CODE',
    title: 'Codex / Claude 状态提醒',
    summary: '追踪开发 Agent 的完成、等待确认和异常状态。',
    triggerLabel: '查看 Agent 任务',
    status: 'ready',
    tone: 'cyan',
  },
  async execute() {
    return {
      title: 'Agent 实时监控已打开',
      detail: '可发现并配置 Codex、Claude Code 和 WorkBuddy 的全局 Hook。',
      tone: 'cyan',
      source: 'live',
    };
  },
};

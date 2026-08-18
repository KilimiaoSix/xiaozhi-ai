import type { FeatureModule } from '../../core/types';

export const gestureApprovalModule: FeatureModule = {
  definition: {
    id: 'gesture-approval',
    code: 'GEST',
    title: '手势审批 Agent 权限',
    summary: '把高风险工具调用交给手势确认，让授权动作可见、可控。',
    triggerLabel: '模拟审批',
    status: 'placeholder',
    tone: 'amber',
  },
  async execute() {
    return {
      title: '收到权限审批手势 · Mock',
      detail: '已预留摄像头手势识别和 Agent 审批回执入口，不执行真实授权。',
      tone: 'amber',
      source: 'mock',
    };
  },
};

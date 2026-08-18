export type FeatureStatus = 'placeholder' | 'ready' | 'attention';

export type FeatureTone = 'cyan' | 'violet' | 'amber' | 'coral';

export interface FeatureDefinition {
  id: string;
  code: string;
  title: string;
  summary: string;
  triggerLabel: string;
  status: FeatureStatus;
  tone: FeatureTone;
}

export const featureCatalog: FeatureDefinition[] = [
  {
    id: 'identity-welcome',
    code: 'FACE',
    title: '身份识别与情绪欢迎',
    summary: '检测到熟悉面孔后，用匹配当下状态的方式打招呼。',
    triggerLabel: '模拟识别',
    status: 'placeholder',
    tone: 'cyan',
  },
  {
    id: 'feishu-briefing',
    code: 'LARK',
    title: '飞书待办与日程简报',
    summary: '把今天最重要的会议、待办和时间冲突压缩成一分钟简报。',
    triggerLabel: '模拟简报',
    status: 'placeholder',
    tone: 'violet',
  },
  {
    id: 'coding-agent-status',
    code: 'CODE',
    title: 'Codex / Claude 状态提醒',
    summary: '追踪开发 Agent 的完成、等待确认和异常状态。',
    triggerLabel: '模拟完成',
    status: 'placeholder',
    tone: 'cyan',
  },
  {
    id: 'gesture-approval',
    code: 'GEST',
    title: '手势审批 Agent 权限',
    summary: '把高风险工具调用交给手势确认，让授权动作可见、可控。',
    triggerLabel: '模拟审批',
    status: 'placeholder',
    tone: 'amber',
  },
  {
    id: 'focus-mode',
    code: 'FOCUS',
    title: '专注模式与分心提醒',
    summary: '记录专注周期，在偏离任务时给出轻量提醒。',
    triggerLabel: '开始专注',
    status: 'placeholder',
    tone: 'violet',
  },
  {
    id: 'away-messages',
    code: 'AWAY',
    title: '离席状态与同事留言',
    summary: '识别离席并替你守住工位，收下访客的简短留言。',
    triggerLabel: '模拟离席',
    status: 'placeholder',
    tone: 'amber',
  },
  {
    id: 'return-summary',
    code: 'BACK',
    title: '返岗信息汇总',
    summary: '回来后一次讲清错过的留言、告警和 Agent 进展。',
    triggerLabel: '模拟返岗',
    status: 'placeholder',
    tone: 'cyan',
  },
  {
    id: 'incident-assistant',
    code: 'ALERT',
    title: '线上告警与诊断协助',
    summary: '聚合告警上下文，给出排查顺序并联动开发 Agent。',
    triggerLabel: '模拟告警',
    status: 'placeholder',
    tone: 'coral',
  },
];

import {
  AWAY_EVENT_KINDS,
  AWAY_SEVERITY_CRITICAL,
  type AwayEventItem,
} from './types';

/**
 * 分桶与服务端 away_ledger._bucket_of 一比一对齐：
 * 严重告警 > 等待操作 > 已完成/失败 > 留言 > 普通消息。
 *
 * 这个顺序是需求定死的播报优先级，机器人念的三句话就是按它压出来的；
 * 桌面端照同一个序展开，用户听到的和看到的才是同一件事的同一个排序。
 * 服务端保证 items 已按此优先级排好且桶内保持记账时间先后，这里不再重排，
 * 只做分组——重排会让「桶内时间先后」这条服务端约定在桌面端悄悄失效。
 */
export type AwayBucketId =
  | 'critical'
  | 'needs_user'
  | 'agent_result'
  | 'visitor'
  | 'generic';

export const AWAY_BUCKET_ORDER: readonly AwayBucketId[] = [
  'critical',
  'needs_user',
  'agent_result',
  'visitor',
  'generic',
];

export const AWAY_BUCKET_LABELS: Record<AwayBucketId, string> = {
  critical: '严重告警',
  needs_user: '等待你操作',
  agent_result: 'Agent 任务结果',
  visitor: '同事留言',
  generic: '普通消息',
};

export const bucketOfAwayEvent = (item: AwayEventItem): AwayBucketId => {
  if (item.kind === AWAY_EVENT_KINDS.incident
    && item.severity === AWAY_SEVERITY_CRITICAL) {
    return 'critical';
  }
  if (item.kind === AWAY_EVENT_KINDS.agentNeedsUser) return 'needs_user';
  if (item.kind === AWAY_EVENT_KINDS.agentCompleted
    || item.kind === AWAY_EVENT_KINDS.agentFailed) {
    return 'agent_result';
  }
  if (item.kind === AWAY_EVENT_KINDS.visitorMessage) return 'visitor';
  // 未知 kind 与非严重告警一并算「普通消息」，排在最后，绝不丢
  return 'generic';
};

export interface AwayBucketGroup {
  id: AwayBucketId;
  label: string;
  items: AwayEventItem[];
}

export const groupAwayEvents = (items: AwayEventItem[]): AwayBucketGroup[] => {
  const buckets = new Map<AwayBucketId, AwayEventItem[]>();
  for (const item of items) {
    const id = bucketOfAwayEvent(item);
    const group = buckets.get(id);
    if (group) group.push(item);
    else buckets.set(id, [item]);
  }
  return AWAY_BUCKET_ORDER
    .filter((id) => buckets.has(id))
    .map((id) => ({
      id,
      label: AWAY_BUCKET_LABELS[id],
      items: buckets.get(id) ?? [],
    }));
};

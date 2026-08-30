import type { FeatureModule } from '../../core/types';
import { groupAwayEvents } from './awayBuckets';
import {
  awaySummaryDesktopGateway,
  type AwaySummaryDesktopGateway,
} from './services/awaySummaryDesktopGateway';

const definition = {
  id: 'away-summary',
  code: 'BACK',
  title: '返岗信息汇总',
  summary: '离席期间的告警、待你确认与同事留言，按播报优先级一次看全。',
  triggerLabel: '查看返岗汇总',
  status: 'ready' as const,
  tone: 'cyan' as const,
};

export const createAwaySummaryModule = (
  gateway: Pick<AwaySummaryDesktopGateway, 'getSummary'> = awaySummaryDesktopGateway,
): FeatureModule => ({
  definition,
  async execute() {
    try {
      const summary = await gateway.getSummary();
      const groups = groupAwayEvents(summary.items);
      const top = groups[0];
      return {
        title: summary.count > 0
          ? `${summary.count} 条待汇总`
          : '没有待汇总的事项',
        detail: top
          ? `最优先：${top.label}（${top.items.length} 条）。详情见返岗页。`
          : '离席期间没有发生需要汇总的事，机器人也不会再念。',
        // 有事才用 amber 提醒；安静时用 cyan，别让卡片常年亮着制造疲劳
        tone: summary.count > 0 ? 'amber' : 'cyan',
        source: 'live',
      };
    } catch (error) {
      return {
        title: '返岗汇总拉取失败',
        detail: error instanceof Error ? error.message : '本地 Server 不可用。',
        tone: 'coral',
        source: 'live',
      };
    }
  },
});

export const awaySummaryModule = createAwaySummaryModule();

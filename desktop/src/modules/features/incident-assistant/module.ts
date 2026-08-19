import type { FeatureModule } from '../../core/types';
import {
  incidentDesktopGateway,
  type IncidentDesktopGateway,
} from './services/incidentDesktopGateway';

const definition = {
  id: 'incident-assistant',
  code: 'ALERT',
  title: '线上告警与诊断协助',
  summary: '两条告警链路的合并列表：标记处理、触发只读诊断、查看时间线。',
  triggerLabel: '查看告警',
  status: 'ready' as const,
  tone: 'coral' as const,
};

export const createIncidentAssistantModule = (
  gateway: Pick<IncidentDesktopGateway, 'list'> = incidentDesktopGateway,
): FeatureModule => ({
  definition,
  async execute() {
    try {
      const { incidents } = await gateway.list();
      const active = incidents.filter((item) => item.state !== 'recovered').length;
      const diagnosing = incidents.filter(
        (item) => item.diagnosis?.state === 'running',
      ).length;
      return {
        title: active > 0 ? `${active} 条告警待处理` : '暂无活跃告警',
        detail: `今日共 ${incidents.length} 条，诊断中 ${diagnosing} 条。详情见告警页。`,
        // coral 提醒有事要看；安静时用 cyan，别让卡片常年红着制造疲劳
        tone: active > 0 ? 'coral' : 'cyan',
        source: 'live',
      };
    } catch (error) {
      return {
        title: '告警列表拉取失败',
        detail: error instanceof Error ? error.message : '本地 Server 不可用。',
        tone: 'coral',
        source: 'live',
      };
    }
  },
});

export const incidentAssistantModule = createIncidentAssistantModule();

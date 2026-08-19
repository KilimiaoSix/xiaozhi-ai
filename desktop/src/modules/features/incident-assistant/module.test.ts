import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { createIncidentAssistantModule } from './module';
import type { IncidentSummary } from './types';

const incident = (overrides: Partial<IncidentSummary> = {}): IncidentSummary => ({
  id: 'demo-1',
  source: 'incident',
  service: 'demo-api',
  severity: 'P1',
  title: '接口错误率升高',
  message: '',
  state: 'firing',
  repeatCount: 1,
  firstSeenAt: '2026-08-19T10:00:00',
  lastSeenAt: '2026-08-19T10:00:00',
  recoveredAt: null,
  announced: true,
  acknowledged: false,
  simulated: false,
  diagnosis: null,
  timeline: [],
  ...overrides,
});

const gatewayWith = (incidents: IncidentSummary[]) => ({
  list: async () => ({ date: '2026-08-19', incidents }),
  ack: async () => ({ acknowledged: true }),
  diagnose: async () => ({ accepted: true }),
});

describe('incidentAssistantModule', () => {
  it('卡片已做实：status 为 ready', () => {
    const module = createIncidentAssistantModule(gatewayWith([]));
    expect(module.definition.id).toBe('incident-assistant');
    expect(module.definition.status).toBe('ready');
  });

  it('execute 返回真实摘要：活跃数 / 今日总数 / 诊断中数', async () => {
    const module = createIncidentAssistantModule(gatewayWith([
      incident({ id: 'a', state: 'firing' }),
      incident({ id: 'b', state: 'observing' }),
      incident({ id: 'c', state: 'recovered', recoveredAt: '2026-08-19T09:00:00' }),
      incident({
        id: 'd',
        state: 'firing',
        diagnosis: { state: 'running', summary: '', finishedAt: null },
      }),
    ]));

    const result = await module.execute(createFeatureTestContext());

    expect(result.source).toBe('live');
    expect(result.tone).toBe('coral');
    expect(result.title).toContain('3 条告警待处理');
    expect(result.detail).toContain('今日共 4 条');
    expect(result.detail).toContain('诊断中 1 条');
  });

  it('没有活跃告警时给出安静文案', async () => {
    const module = createIncidentAssistantModule(gatewayWith([
      incident({ id: 'c', state: 'recovered', recoveredAt: '2026-08-19T09:00:00' }),
    ]));

    const result = await module.execute(createFeatureTestContext());

    expect(result.tone).toBe('cyan');
    expect(result.title).toContain('暂无活跃告警');
    expect(result.detail).toContain('今日共 1 条');
  });

  it('Server 不可用时降级为失败结果而不是抛异常', async () => {
    const module = createIncidentAssistantModule({
      list: async () => {
        throw new Error('本地 Server 当前不可用');
      },
    });

    const result = await module.execute(createFeatureTestContext());

    expect(result.source).toBe('live');
    expect(result.tone).toBe('coral');
    expect(result.detail).toContain('本地 Server 当前不可用');
  });
});

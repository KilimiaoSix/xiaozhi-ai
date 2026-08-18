import { describe, expect, it } from 'vitest';

import type { FeatureRuntimeContext } from '../core/types';
import { PlaceholderServerGateway } from '../../services/server/placeholderServerGateway';
import { featureModules, featureRegistry } from './index';

const expectedIds = [
  'identity-welcome',
  'feishu-briefing',
  'coding-agent-status',
  'gesture-approval',
  'focus-mode',
  'away-messages',
  'return-summary',
  'incident-assistant',
];

const context: FeatureRuntimeContext = {
  now: () => new Date('2026-08-18T08:00:00+08:00'),
  server: new PlaceholderServerGateway('http://192.168.1.2:8003'),
};

describe('默认功能模块', () => {
  it('按固定顺序注册八个独立模块', () => {
    expect(featureModules.map(({ definition }) => definition.id)).toEqual(expectedIds);
    expect(featureRegistry.list()).toHaveLength(8);
  });

  it('每个模块都通过注册中心返回场景化 Mock 结果', async () => {
    const results = await Promise.all(
      expectedIds.map((id) => featureRegistry.execute(id, context)),
    );

    expect(results.every((result) => result.source === 'mock')).toBe(true);
    expect(results.every((result) => result.title.length > 0 && result.detail.length > 0)).toBe(true);
    expect(new Set(results.map(({ title }) => title)).size).toBe(8);

    results.forEach((result, index) => {
      expect(result.tone).toBe(featureModules[index].definition.tone);
    });
  });
});

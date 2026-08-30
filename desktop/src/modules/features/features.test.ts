import { describe, expect, it } from 'vitest';

import type { FeatureRuntimeContext } from '../core/types';
import { PlaceholderServerGateway } from '../../services/server/placeholderServerGateway';
import {
  HIDDEN_FEATURE_IDS,
  featureModules,
  featureRegistry,
  visibleFeatureCatalog,
} from './index';

const expectedIds = [
  'identity-welcome',
  'feishu-briefing',
  'coding-agent-status',
  'gesture-approval',
  'focus-mode',
  'away-messages',
  'away-summary',
  'incident-assistant',
  'return-summary',
];

const context: FeatureRuntimeContext = {
  now: () => new Date('2026-08-18T08:00:00+08:00'),
  server: new PlaceholderServerGateway('http://192.168.1.2:8003'),
};

describe('默认功能模块', () => {
  it('按固定顺序注册九个独立模块（含仍在注册表里但不上屏的占位模块）', () => {
    expect(featureModules.map(({ definition }) => definition.id)).toEqual(expectedIds);
    expect(featureRegistry.list()).toHaveLength(9);
  });

  it('四个 Mock 占位模块不进 UI 目录，代码仍保留在注册表里', () => {
    expect([...HIDDEN_FEATURE_IDS].sort()).toEqual([
      'away-messages',
      'gesture-approval',
      'identity-welcome',
      'return-summary',
    ]);

    const visibleIds = visibleFeatureCatalog.map(({ id }) => id);
    expect(visibleIds).toEqual([
      'feishu-briefing',
      'coding-agent-status',
      'focus-mode',
      'away-summary',
      'incident-assistant',
    ]);
    for (const hidden of HIDDEN_FEATURE_IDS) {
      // 隐藏只是不上屏：模块本身还能被注册表取到，execute 也照常可用
      expect(featureRegistry.get(hidden).definition.id).toBe(hidden);
    }
  });

  it('上屏的卡片全部是已接入的真实入口，没有 placeholder', () => {
    expect(visibleFeatureCatalog.every((feature) => feature.status === 'ready')).toBe(true);
    expect(visibleFeatureCatalog.some((feature) => feature.triggerLabel.includes('模拟')))
      .toBe(false);
  });

  it('隐藏的四个模块保持场景化 Mock，其余模块返回实时入口', async () => {
    const results = await Promise.all(
      expectedIds.map(async (id) => [id, await featureRegistry.execute(id, context)] as const),
    );

    for (const [id, result] of results) {
      expect(result.source).toBe(HIDDEN_FEATURE_IDS.has(id) ? 'mock' : 'live');
      expect(result.title.length).toBeGreaterThan(0);
      expect(result.detail.length).toBeGreaterThan(0);
    }
    expect(new Set(results.map(([, result]) => result.title)).size).toBe(9);
  });
});

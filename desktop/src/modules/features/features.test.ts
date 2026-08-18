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

  it('编码 Agent 和专注模式返回实时入口，其余模块保持场景化 Mock', async () => {
    const results = await Promise.all(
      expectedIds.map((id) => featureRegistry.execute(id, context)),
    );
    const liveIndexes = [2, 4]; // coding-agent-status, focus-mode

    liveIndexes.forEach((index) => expect(results[index].source).toBe('live'));
    expect(results.filter((_, index) => !liveIndexes.includes(index))
      .every((result) => result.source === 'mock')).toBe(true);
    expect(results.every((result) => result.title.length > 0 && result.detail.length > 0)).toBe(true);
    expect(new Set(results.map(({ title }) => title)).size).toBe(8);

    // 专注模式在无渲染进程（无 window.xiaofei）时找不到网关，走异常兜底分支，
    // 结果 tone 会变成 coral 而不是卡片静态定义的 violet，因此单独断言。
    results.forEach((result, index) => {
      if (index === 4) return;
      expect(result.tone).toBe(featureModules[index].definition.tone);
    });
    expect(results[4].tone).toBe('coral');
  });
});

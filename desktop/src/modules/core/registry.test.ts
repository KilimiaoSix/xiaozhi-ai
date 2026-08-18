import { describe, expect, it } from 'vitest';

import type { FeatureModule, FeatureRuntimeContext } from './types';
import { FeatureRegistry } from './registry';
import { PlaceholderServerGateway } from '../../services/server/placeholderServerGateway';

const createModule = (id: string): FeatureModule => ({
  definition: {
    id,
    code: id.toUpperCase(),
    title: `模块 ${id}`,
    summary: '测试模块',
    triggerLabel: '执行',
    status: 'placeholder',
    tone: 'cyan',
  },
  async execute() {
    return {
      title: `已执行 ${id}`,
      detail: '测试结果',
      tone: 'cyan',
      source: 'mock',
    };
  },
});

const context: FeatureRuntimeContext = {
  now: () => new Date('2026-08-18T08:00:00+08:00'),
  server: new PlaceholderServerGateway(),
};

describe('FeatureRegistry', () => {
  it('按模块 ID 执行已注册模块', async () => {
    const registry = new FeatureRegistry([createModule('alpha')]);

    await expect(registry.execute('alpha', context)).resolves.toMatchObject({
      title: '已执行 alpha',
      source: 'mock',
    });
  });

  it('拒绝重复的模块 ID', () => {
    expect(() => new FeatureRegistry([createModule('alpha'), createModule('alpha')]))
      .toThrow('功能模块 ID 重复: alpha');
  });

  it('执行未知模块时返回明确错误', async () => {
    const registry = new FeatureRegistry([]);

    await expect(registry.execute('missing', context))
      .rejects.toThrow('未找到功能模块: missing');
  });
});

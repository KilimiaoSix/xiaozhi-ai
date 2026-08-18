import { describe, expect, it } from 'vitest';

import { featureRegistry } from './features';
import { featureRuntimeContext, serverGateway } from './runtime';

describe('默认功能运行时', () => {
  it('为模块执行注入时钟和安全的 Server Gateway', async () => {
    expect(featureRuntimeContext.now()).toBeInstanceOf(Date);
    expect(featureRuntimeContext.server).toBe(serverGateway);

    await expect(featureRegistry.execute('identity-welcome', featureRuntimeContext))
      .resolves.toMatchObject({ source: 'mock' });
  });
});

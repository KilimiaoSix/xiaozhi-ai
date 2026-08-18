import { describe, expect, it } from 'vitest';

import { PlaceholderServerGateway } from './placeholderServerGateway';

describe('PlaceholderServerGateway', () => {
  it('保存去除首尾空白后的 Server 地址', () => {
    const gateway = new PlaceholderServerGateway();

    expect(gateway.setBaseUrl('  http://192.168.1.2:8003  '))
      .toBe('http://192.168.1.2:8003');
    expect(gateway.getBaseUrl()).toBe('http://192.168.1.2:8003');

    expect(gateway.setBaseUrl('   ')).toBe('');
    expect(gateway.getBaseUrl()).toBe('');
  });

  it('占位阶段拒绝发送真实 HTTP 请求', async () => {
    const gateway = new PlaceholderServerGateway('http://192.168.1.2:8003');

    await expect(gateway.request({ method: 'GET', path: '/health' }))
      .rejects.toThrow('Server HTTP 尚未接入');
  });
});

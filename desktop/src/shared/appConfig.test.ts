import { describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_APP_CONFIG,
  configOf,
  parseAppConfigPatch,
  resolveAppConfig,
} from './appConfig';

describe('resolveAppConfig', () => {
  it('三层优先级逐字段独立：env 覆盖 file，file 覆盖默认值', () => {
    const resolution = resolveAppConfig({
      file: { serverUrl: 'http://192.168.1.9:8003', deviceId: 'file-device' },
      env: { DESKPET_DEVICE_ID: 'env-device' },
    });

    expect(resolution.serverUrl).toMatchObject({
      value: 'http://192.168.1.9:8003',
      source: 'file',
    });
    expect(resolution.deviceId).toMatchObject({
      value: 'env-device',
      source: 'env',
      envVar: 'DESKPET_DEVICE_ID',
      fileValue: 'file-device',
    });
    expect(resolution.authToken).toMatchObject({
      value: DEFAULT_APP_CONFIG.authToken,
      source: 'default',
    });
  });

  it('没有任何配置时落到默认地址 http://127.0.0.1:8003', () => {
    const resolution = resolveAppConfig();

    expect(configOf(resolution)).toEqual({
      serverUrl: 'http://127.0.0.1:8003',
      deviceId: '',
      authToken: '',
    });
    expect(resolution.serverUrl.source).toBe('default');
  });

  it('兼容摄像头链路的旧变量名 XIAOFEI_SERVER_URL 与 XIAOFEI_SERVER_AUTH_TOKEN', () => {
    const resolution = resolveAppConfig({
      env: {
        XIAOFEI_SERVER_URL: 'http://10.0.0.7:8003',
        XIAOFEI_SERVER_AUTH_TOKEN: 'camera-token',
      },
    });

    expect(resolution.serverUrl).toMatchObject({
      value: 'http://10.0.0.7:8003',
      source: 'env',
      envVar: 'XIAOFEI_SERVER_URL',
    });
    expect(resolution.authToken).toMatchObject({
      value: 'camera-token',
      source: 'env',
      envVar: 'XIAOFEI_SERVER_AUTH_TOKEN',
    });
  });

  it('两个 serverUrl 变量同时存在时 DESKPET_SERVER 赢，并且明说另一个被忽略', () => {
    const onWarn = vi.fn();
    const resolution = resolveAppConfig({
      env: {
        DESKPET_SERVER: 'http://10.0.0.1:8003',
        XIAOFEI_SERVER_URL: 'http://10.0.0.2:8003',
      },
      onWarn,
    });

    expect(resolution.serverUrl).toMatchObject({
      value: 'http://10.0.0.1:8003',
      envVar: 'DESKPET_SERVER',
    });
    const warning = String(onWarn.mock.calls[0]?.[0]);
    expect(warning).toContain('DESKPET_SERVER');
    expect(warning).toContain('XIAOFEI_SERVER_URL');
  });

  it('空串与纯空白的环境变量视作未设置，不会把地址覆盖成空', () => {
    const resolution = resolveAppConfig({
      file: { serverUrl: 'http://192.168.1.9:8003' },
      env: { DESKPET_SERVER: '   ' },
    });

    expect(resolution.serverUrl).toMatchObject({
      value: 'http://192.168.1.9:8003',
      source: 'file',
    });
  });

  it('取值前后去空白，避免复制粘贴带进来的换行', () => {
    const resolution = resolveAppConfig({
      env: { DESKPET_SERVER: ' http://10.0.0.1:8003\n' },
    });

    expect(resolution.serverUrl.value).toBe('http://10.0.0.1:8003');
  });

  it('serverUrl 尾斜杠归一：文件层去掉尾斜杠，避免下游拼出双斜杠 404', () => {
    const resolution = resolveAppConfig({
      file: { serverUrl: 'http://192.168.1.20:8003/' },
    });

    expect(resolution.serverUrl).toMatchObject({
      value: 'http://192.168.1.20:8003',
      source: 'file',
      fileValue: 'http://192.168.1.20:8003',
    });
  });

  it('serverUrl 尾斜杠归一：env 层同样生效，且多余斜杠一并去掉', () => {
    const resolution = resolveAppConfig({
      env: { DESKPET_SERVER: 'http://10.0.0.1:8003///' },
    });

    expect(resolution.serverUrl.value).toBe('http://10.0.0.1:8003');
  });

  it('尾斜杠归一只作用于 serverUrl，authToken 原样保留（哪怕恰好以斜杠结尾）', () => {
    const resolution = resolveAppConfig({
      file: { authToken: 'token/with/slash/' },
    });

    expect(resolution.authToken.value).toBe('token/with/slash/');
  });
});

describe('parseAppConfigPatch', () => {
  it('接受合法的 http/https 地址', () => {
    expect(parseAppConfigPatch({ serverUrl: 'http://127.0.0.1:8003' }))
      .toEqual({ serverUrl: 'http://127.0.0.1:8003' });
    expect(parseAppConfigPatch({ serverUrl: 'https://10.0.0.1:8003' }))
      .toEqual({ serverUrl: 'https://10.0.0.1:8003' });
  });

  it('允许清空 serverUrl（空字符串不校验协议头，落回默认值）', () => {
    expect(parseAppConfigPatch({ serverUrl: '' })).toEqual({ serverUrl: '' });
  });

  it('拒绝缺协议头的地址，报出可读的中文错误，且不改变其它字段的解析', () => {
    expect(() => parseAppConfigPatch({ serverUrl: 'localhost:8003' }))
      .toThrow(/http|https|协议/);
  });

  it('拒绝纯 host:port 形式（数字开头解析不出合法 scheme）的地址', () => {
    expect(() => parseAppConfigPatch({ serverUrl: '192.168.1.20:8003' }))
      .toThrow(/http|https|协议|无效/);
  });

  it('拒绝非 http/https 协议（例如 ftp）', () => {
    expect(() => parseAppConfigPatch({ serverUrl: 'ftp://192.168.1.20:8003' }))
      .toThrow(/http|https|协议/);
  });
});

import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import { AppConfigStore } from './appConfigStore';
import {
  CONFIG_CHANNELS,
  registerConfigIpc,
  type ConfigIpcMainLike,
} from './registerConfigIpc';

const directories: string[] = [];

const makeHarness = async (env: Record<string, string | undefined> = {}) => {
  const directory = await mkdtemp(path.join(tmpdir(), 'launchcrush-config-ipc-'));
  directories.push(directory);
  const store = new AppConfigStore({ filePath: path.join(directory, 'config.json'), env });
  await store.load();
  const handlers = new Map<string, (...args: unknown[]) => unknown>();
  const removed: string[] = [];
  const ipcMain: ConfigIpcMainLike = {
    handle: (channel, handler) => { handlers.set(channel, handler); },
    removeHandler: (channel) => { removed.push(channel); },
  };
  const cleanup = registerConfigIpc({ ipcMain, store });
  return { store, handlers, removed, cleanup };
};

afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) =>
    rm(directory, { recursive: true, force: true })));
});

describe('registerConfigIpc', () => {
  it('读取返回每个字段的生效值与来源', async () => {
    const { handlers, cleanup } = await makeHarness({ DESKPET_DEVICE_ID: 'env-device' });

    await expect(handlers.get(CONFIG_CHANNELS.get)!({})).resolves.toMatchObject({
      ok: true,
      value: {
        serverUrl: { value: 'http://127.0.0.1:8003', source: 'default' },
        deviceId: { value: 'env-device', source: 'env', envVar: 'DESKPET_DEVICE_ID' },
        authToken: { value: '', source: 'default' },
      },
    });

    cleanup();
  });

  it('写入落盘并在同一次调用里回传新的生效值', async () => {
    const { store, handlers, cleanup } = await makeHarness();

    const result = await handlers.get(CONFIG_CHANNELS.update)!(
      {},
      { serverUrl: 'http://192.168.1.20:8003' },
    ) as { ok: boolean; value: { serverUrl: { value: string; source: string } } };

    expect(result).toMatchObject({
      ok: true,
      value: { serverUrl: { value: 'http://192.168.1.20:8003', source: 'file' } },
    });
    expect(store.get().serverUrl).toBe('http://192.168.1.20:8003');

    cleanup();
  });

  it('拒绝未知字段与非字符串值，错误以信封形式回传', async () => {
    const { handlers, cleanup } = await makeHarness();

    await expect(handlers.get(CONFIG_CHANNELS.update)!({}, { nope: 'x' }))
      .resolves.toMatchObject({ ok: false });
    await expect(handlers.get(CONFIG_CHANNELS.update)!({}, { deviceId: 7 }))
      .resolves.toMatchObject({ ok: false });
    await expect(handlers.get(CONFIG_CHANNELS.update)!({}, 'not-an-object'))
      .resolves.toMatchObject({ ok: false });

    cleanup();
  });

  it('serverUrl 缺协议头时落盘前就拒绝：信封报错可读，且不落盘、生效值不变', async () => {
    const { store, handlers, cleanup } = await makeHarness();

    const result = await handlers.get(CONFIG_CHANNELS.update)!(
      {},
      { serverUrl: 'localhost:8003' },
    ) as { ok: boolean; error?: string };

    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/http|https|协议/);
    expect(store.get().serverUrl).toBe('http://127.0.0.1:8003');

    cleanup();
  });

  it('cleanup 注销全部 channel', async () => {
    const { handlers, removed, cleanup } = await makeHarness();

    cleanup();

    expect(removed.sort()).toEqual([...handlers.keys()].sort());
  });
});

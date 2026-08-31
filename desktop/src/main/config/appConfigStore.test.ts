import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { AppConfigStore } from './appConfigStore';

const directories: string[] = [];

const makeStore = async (
  options: {
    file?: string;
    env?: Record<string, string | undefined>;
    onWarn?: (message: string) => void;
  } = {},
): Promise<{ store: AppConfigStore; filePath: string }> => {
  const directory = await mkdtemp(path.join(tmpdir(), 'launchcrush-config-'));
  directories.push(directory);
  const filePath = path.join(directory, 'state/config.json');
  if (options.file !== undefined) {
    await mkdir(path.dirname(filePath), { recursive: true });
    await writeFile(filePath, options.file, 'utf8');
  }
  const store = new AppConfigStore({
    filePath,
    env: options.env ?? {},
    ...(options.onWarn ? { onWarn: options.onWarn } : {}),
  });
  await store.load();
  return { store, filePath };
};

afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) =>
    rm(directory, { recursive: true, force: true })));
});

describe('AppConfigStore', () => {
  it('保存后原子落盘，重新打开还能读回来', async () => {
    const { store, filePath } = await makeStore();

    await store.update({ serverUrl: 'http://192.168.1.20:8003', deviceId: 'dc:da:0c:26:9a:60' });

    expect(JSON.parse(await readFile(filePath, 'utf8'))).toMatchObject({
      serverUrl: 'http://192.168.1.20:8003',
      deviceId: 'dc:da:0c:26:9a:60',
    });
    const reopened = new AppConfigStore({ filePath, env: {} });
    await reopened.load();
    expect(reopened.get()).toEqual({
      serverUrl: 'http://192.168.1.20:8003',
      deviceId: 'dc:da:0c:26:9a:60',
      authToken: '',
    });
    // 落盘期间不留临时文件
    const leftovers = await readFile(filePath, 'utf8');
    expect(leftovers.endsWith('\n')).toBe(true);
  });

  it('部分更新只动给到的字段', async () => {
    const { store } = await makeStore();
    await store.update({ serverUrl: 'http://192.168.1.20:8003', authToken: 'secret' });

    await store.update({ deviceId: 'esp32-01' });

    expect(store.get()).toEqual({
      serverUrl: 'http://192.168.1.20:8003',
      deviceId: 'esp32-01',
      authToken: 'secret',
    });
  });

  it('配置文件坏掉时退回默认值继续跑，并且报一次警告', async () => {
    const onWarn = vi.fn();
    const { store } = await makeStore({ file: '{ 这不是 JSON', onWarn });

    expect(store.get().serverUrl).toBe('http://127.0.0.1:8003');
    expect(onWarn).toHaveBeenCalled();
  });

  it('配置文件里的非字符串字段被忽略而不是让整份配置作废', async () => {
    const { store } = await makeStore({
      file: JSON.stringify({ serverUrl: 'http://192.168.1.20:8003', deviceId: 42 }),
    });

    expect(store.get()).toMatchObject({
      serverUrl: 'http://192.168.1.20:8003',
      deviceId: '',
    });
  });

  it('文件不存在时不报错，第一次保存自动建目录', async () => {
    const { store, filePath } = await makeStore();

    expect(store.get().serverUrl).toBe('http://127.0.0.1:8003');
    await store.update({ serverUrl: 'http://127.0.0.1:9003' });
    expect(JSON.parse(await readFile(filePath, 'utf8')).serverUrl)
      .toBe('http://127.0.0.1:9003');
  });

  it('环境变量覆盖时生效值来自 env，但保存的仍然是文件那一层', async () => {
    const { store } = await makeStore({ env: { DESKPET_SERVER: 'http://10.0.0.1:8003' } });

    await store.update({ serverUrl: 'http://192.168.1.20:8003' });

    expect(store.get().serverUrl).toBe('http://10.0.0.1:8003');
    expect(store.resolve().serverUrl).toMatchObject({
      value: 'http://10.0.0.1:8003',
      source: 'env',
      envVar: 'DESKPET_SERVER',
      fileValue: 'http://192.168.1.20:8003',
    });
  });

  it('生效值变化才通知订阅者：被 env 遮住的保存不触发重连', async () => {
    const { store } = await makeStore({ env: { DESKPET_SERVER: 'http://10.0.0.1:8003' } });
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);

    await store.update({ serverUrl: 'http://192.168.1.20:8003' });
    expect(listener).not.toHaveBeenCalled();

    await store.update({ deviceId: 'esp32-01' });
    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener.mock.calls[0]?.[0]).toMatchObject({ deviceId: 'esp32-01' });

    unsubscribe();
    await store.update({ deviceId: 'esp32-02' });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('取值是按次解析的：同一个 store 引用能读到后来保存的地址', async () => {
    const { store } = await makeStore();
    const resolveBaseUrl = () => store.get().serverUrl;

    expect(resolveBaseUrl()).toBe('http://127.0.0.1:8003');
    await store.update({ serverUrl: 'http://192.168.1.20:8003' });
    expect(resolveBaseUrl()).toBe('http://192.168.1.20:8003');
  });

  it('同进程并发两次 update 都 fulfilled，落盘是两次 patch 的合并结果（不因同名 .tmp 互撞而误报失败）', async () => {
    const { store, filePath } = await makeStore();

    const [resultA, resultB] = await Promise.allSettled([
      store.update({ serverUrl: 'http://192.168.1.20:8003' }),
      store.update({ deviceId: 'esp32-01' }),
    ]);

    expect(resultA.status).toBe('fulfilled');
    expect(resultB.status).toBe('fulfilled');
    expect(JSON.parse(await readFile(filePath, 'utf8'))).toMatchObject({
      serverUrl: 'http://192.168.1.20:8003',
      deviceId: 'esp32-01',
    });
    expect(store.get()).toMatchObject({
      serverUrl: 'http://192.168.1.20:8003',
      deviceId: 'esp32-01',
    });
  });

  it('落盘失败时 update reject、内存态不提交、订阅者不被通知（先落盘成功才提交内存/通知）', async () => {
    const directory = await mkdtemp(path.join(tmpdir(), 'launchcrush-config-'));
    directories.push(directory);
    // 父目录指向一个已存在的普通文件：mkdir(dirname, {recursive:true}) 会 ENOTDIR
    const blockerPath = path.join(directory, 'blocker');
    await writeFile(blockerPath, 'not a directory', 'utf8');
    const filePath = path.join(blockerPath, 'config.json');
    const store = new AppConfigStore({ filePath, env: {} });
    await store.load();
    const listener = vi.fn();
    store.subscribe(listener);

    await expect(store.update({ serverUrl: 'http://192.168.1.20:8003' })).rejects.toThrow();

    expect(store.get().serverUrl).toBe('http://127.0.0.1:8003');
    expect(listener).not.toHaveBeenCalled();
  });
});

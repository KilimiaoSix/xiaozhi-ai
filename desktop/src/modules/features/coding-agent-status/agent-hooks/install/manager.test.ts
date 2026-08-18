import { mkdtemp, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import type { AgentSource } from '../contracts';
import { AgentHookManager } from './manager';

const temporaryDirectories: string[] = [];

const createHome = async (): Promise<string> => {
  const value = await mkdtemp(path.join(tmpdir(), 'launchcrush-hooks-'));
  temporaryDirectories.push(value);
  return value;
};

const writeText = async (filePath: string, content: string): Promise<void> => {
  await mkdir(path.dirname(filePath), { recursive: true });
  await writeFile(filePath, content, 'utf8');
};

const createManager = (
  homeDir: string,
  available: Partial<Record<AgentSource, string>>,
  electronPath = '/Applications/工伴.app/Contents/MacOS/工伴',
) => new AgentHookManager({
  homeDir,
  launcherPath: path.join(homeDir, 'Library/Application Support/工伴/hooks/launchcrush-hook'),
  spoolPath: path.join(homeDir, 'Library/Application Support/工伴/agent-hooks'),
  codexTrustMarkerPath: path.join(
    homeDir,
    'Library/Application Support/工伴/agent-hooks/state/codex-trust-required',
  ),
  now: () => new Date('2026-08-18T08:00:00.000Z'),
  resolveExecutable: async (source) => available[source],
  workBuddyAppPaths: [path.join(homeDir, 'Applications/WorkBuddy.app')],
  ensureRunner: () => writeText(
    path.join(homeDir, 'Library/Application Support/工伴/hooks/launchcrush-hook'),
    `runtime=${electronPath}`,
  ),
});

afterEach(async () => {
  const { rm } = await import('node:fs/promises');
  await Promise.all(temporaryDirectories.splice(0).map((directory) =>
    rm(directory, { recursive: true, force: true })));
});

describe('AgentHookManager', () => {
  it('发现 Codex、Claude Code 和 WorkBuddy 的真实配置位置', async () => {
    const homeDir = await createHome();
    await writeText(path.join(homeDir, '.workbuddy/settings.json'), '{}');
    const manager = createManager(homeDir, {
      codex: '/usr/local/bin/codex',
      'claude-code': '/usr/local/bin/claude',
    });

    await expect(manager.detect()).resolves.toEqual([
      expect.objectContaining({
        source: 'codex', available: true, installed: false,
        configPath: path.join(homeDir, '.codex/hooks.json'),
      }),
      expect.objectContaining({
        source: 'claude-code', available: true, installed: false,
        configPath: path.join(homeDir, '.claude/settings.json'),
      }),
      expect.objectContaining({
        source: 'workbuddy', available: true, installed: false,
        configPath: path.join(homeDir, '.workbuddy/settings.json'),
      }),
    ]);
  });

  it('合并配置前备份且重复安装不重复写入', async () => {
    const homeDir = await createHome();
    const configPath = path.join(homeDir, '.codex/hooks.json');
    await writeText(configPath, JSON.stringify({ theme: 'dark' }, null, 2));
    const manager = createManager(homeDir, { codex: '/usr/local/bin/codex' });

    const first = await manager.install('codex');
    const firstContent = await readFile(configPath, 'utf8');
    const second = await manager.install('codex');
    const secondContent = await readFile(configPath, 'utf8');

    expect(first).toMatchObject({ ok: true, installed: true, configPath });
    expect(first.backupPath).toBe(
      `${configPath}.2026-08-18T08-00-00-000Z.launchcrush.bak`,
    );
    await expect(readFile(first.backupPath!, 'utf8')).resolves.toContain('"theme": "dark"');
    expect(second).toMatchObject({ ok: true, installed: true });
    expect(second.backupPath).toBeUndefined();
    expect(secondContent).toBe(firstContent);
    expect(firstContent.match(/launchcrush-agent-hook/g)).toHaveLength(7);
  });

  it('开发版与打包版使用相同 Hook 命令且不会反复刷新 Codex 授权', async () => {
    const homeDir = await createHome();
    const configPath = path.join(homeDir, '.codex/hooks.json');
    const available = { codex: '/usr/local/bin/codex' } as const;
    const development = createManager(homeDir, available, '/dev/Electron');
    const packaged = createManager(homeDir, available, '/Applications/工伴.app/Contents/MacOS/工伴');

    await development.install('codex');
    const developmentConfig = await readFile(configPath, 'utf8');
    await packaged.prepare();
    expect(await readFile(configPath, 'utf8')).toBe(developmentConfig);
    await expect(readFile(
      path.join(homeDir, 'Library/Application Support/工伴/hooks/launchcrush-hook'),
      'utf8',
    )).resolves.toBe('runtime=/Applications/工伴.app/Contents/MacOS/工伴');

    const packagedResult = await packaged.install('codex');
    const packagedConfig = await readFile(configPath, 'utf8');

    expect(packagedResult.backupPath).toBeUndefined();
    expect(packagedConfig).toBe(developmentConfig);
    expect(packagedConfig).not.toContain('/dev/Electron');
    expect(packagedConfig).not.toContain('/Applications/工伴.app/Contents/MacOS/工伴');
  });

  it('Codex Hook 配置变更后等待授权并在收到真实事件后转为监控中', async () => {
    const homeDir = await createHome();
    const manager = createManager(homeDir, { codex: '/usr/local/bin/codex' });

    await expect(manager.install('codex')).resolves.toMatchObject({
      installed: true,
      requiresTrustReview: true,
      message: expect.stringContaining('/hooks'),
    });
    await expect(manager.detect()).resolves.toContainEqual(expect.objectContaining({
      source: 'codex',
      installed: true,
      requiresTrustReview: true,
      message: expect.stringContaining('/hooks'),
    }));

    await manager.markActive('codex');

    await expect(manager.detect()).resolves.toContainEqual(expect.objectContaining({
      source: 'codex',
      installed: true,
      requiresTrustReview: false,
      message: '监控 Hook 已生效',
    }));
  });

  it('识别并刷新指向已删除 worktree 的 WorkBuddy Hook', async () => {
    const homeDir = await createHome();
    const configPath = path.join(homeDir, '.workbuddy/settings.json');
    const staleCommand = [
      'ELECTRON_RUN_AS_NODE=1',
      "'/deleted-worktree/desktop/node_modules/electron/Electron'",
      "'/old/launchcrush-hook.cjs'",
      '--owner launchcrush-agent-hook',
      '--source workbuddy',
      "--spool '/old/agent-hooks'",
    ].join(' ');
    await writeText(configPath, JSON.stringify({
      hooks: {
        Stop: [
          { hooks: [{ type: 'command', command: '/user/notify.sh' }] },
          { hooks: [{ type: 'command', command: staleCommand, timeout: 5 }] },
        ],
      },
    }, null, 2));
    const manager = createManager(homeDir, { workbuddy: '/usr/local/bin/workbuddy' });

    const before = (await manager.detect()).find(({ source }) => source === 'workbuddy');
    expect(before).toMatchObject({
      installed: false,
      message: expect.stringContaining('失效'),
    });

    const installed = await manager.install('workbuddy');
    const refreshed = await readFile(configPath, 'utf8');
    const after = (await manager.detect()).find(({ source }) => source === 'workbuddy');

    expect(installed).toMatchObject({
      ok: true,
      installed: true,
      backupPath: `${configPath}.2026-08-18T08-00-00-000Z.launchcrush.bak`,
      message: expect.stringContaining('刷新'),
    });
    expect(refreshed).not.toContain('/deleted-worktree/');
    expect(refreshed).toContain('Library/Application Support/工伴/hooks/launchcrush-hook');
    expect(refreshed).toContain('/user/notify.sh');
    expect(refreshed.match(/launchcrush-agent-hook/g)).toHaveLength(8);
    expect(after).toMatchObject({ installed: true, message: '监控 Hook 已安装' });
  });

  it('配置 JSON 损坏时拒绝覆盖原文件', async () => {
    const homeDir = await createHome();
    const configPath = path.join(homeDir, '.claude/settings.json');
    await writeText(configPath, '{ broken json');
    const manager = createManager(homeDir, {
      'claude-code': '/usr/local/bin/claude',
    });

    await expect(manager.install('claude-code')).resolves.toMatchObject({
      ok: false,
      installed: false,
      message: expect.stringContaining('JSON'),
    });
    await expect(readFile(configPath, 'utf8')).resolves.toBe('{ broken json');
  });

  it('卸载只删除 launchcrush handler 并保留用户 Hook', async () => {
    const homeDir = await createHome();
    const configPath = path.join(homeDir, '.codex/hooks.json');
    await writeText(configPath, JSON.stringify({
      hooks: {
        Stop: [{ hooks: [{ type: 'command', command: '/user/notify.sh' }] }],
      },
    }, null, 2));
    const manager = createManager(homeDir, { codex: '/usr/local/bin/codex' });
    await manager.install('codex');

    await expect(manager.uninstall('codex')).resolves.toMatchObject({
      ok: true,
      installed: false,
    });
    const uninstalled = await readFile(configPath, 'utf8');
    expect(uninstalled).toContain('/user/notify.sh');
    expect(uninstalled).not.toContain('launchcrush-agent-hook');
  });

  it('未发现工具时不创建配置目录', async () => {
    const homeDir = await createHome();
    const manager = createManager(homeDir, {});

    await expect(manager.install('codex')).resolves.toMatchObject({
      ok: false,
      installed: false,
      message: expect.stringContaining('未发现'),
    });
    await expect(readdir(homeDir)).resolves.toEqual([]);
  });

  it('WorkBuddy 兼容已有的 .codebuddy 配置路径', async () => {
    const homeDir = await createHome();
    await writeText(path.join(homeDir, '.codebuddy/settings.json'), '{}');
    const manager = createManager(homeDir, {});

    const [,, workbuddy] = await manager.detect();
    expect(workbuddy).toMatchObject({
      available: true,
      configPath: path.join(homeDir, '.codebuddy/settings.json'),
    });
  });
});

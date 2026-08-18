import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import { createAgentHooksRuntime } from './createAgentHooksRuntime';

const directories: string[] = [];

const runLauncher = async (
  launcherPath: string,
  spoolPath: string,
  payload: Record<string, unknown>,
): Promise<void> => new Promise((resolve, reject) => {
  const child = spawn(launcherPath, [
    '--owner', 'launchcrush-agent-hook',
    '--source', 'codex',
    '--spool', spoolPath,
  ], { stdio: ['pipe', 'ignore', 'pipe'] });
  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk: string) => { stderr += chunk; });
  child.once('error', reject);
  child.once('close', (code) => {
    if (code === 0) resolve();
    else reject(new Error(`Hook launcher exited with code ${code}: ${stderr}`));
  });
  child.stdin.end(JSON.stringify(payload));
});

afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) =>
    rm(directory, { recursive: true, force: true })));
});

describe('createAgentHooksRuntime', () => {
  it('固定启动器把真实 Codex Hook 事件送入 Runtime 并完成激活', async () => {
    const directory = await mkdtemp(path.join(tmpdir(), 'launchcrush-runtime-factory-'));
    directories.push(directory);
    const homeDir = path.join(directory, 'home');
    const userDataPath = path.join(directory, 'user-data');
    const configPath = path.join(homeDir, '.codex/hooks.json');
    const spoolPath = path.join(userDataPath, 'agent-hooks');
    const launcherPath = path.join(spoolPath, 'launchcrush-hook');
    await mkdir(path.dirname(configPath), { recursive: true });
    await writeFile(configPath, '{}\n', 'utf8');
    const runtime = createAgentHooksRuntime({
      homeDir,
      userDataPath,
      electronPath: process.execPath,
      platform: 'linux',
    });

    await expect(runtime.install('codex')).resolves.toMatchObject({
      installed: true,
      requiresTrustReview: true,
    });
    const config = await readFile(configPath, 'utf8');
    expect(config).toContain(launcherPath);
    expect(config).not.toContain(process.execPath);

    await runLauncher(launcherPath, spoolPath, {
      session_id: 'codex-launcher-integration',
      hook_event_name: 'UserPromptSubmit',
      occurred_at: '2026-08-18T08:00:00.000Z',
      prompt: '验证稳定启动器',
    });
    await runtime.start();

    expect(runtime.getSnapshot()).toMatchObject({
      installations: expect.arrayContaining([expect.objectContaining({
        source: 'codex',
        installed: true,
        requiresTrustReview: false,
      })]),
      primaryTask: {
        source: 'codex',
        sessionId: 'codex-launcher-integration',
        status: 'running',
        title: '验证稳定启动器',
      },
    });
    await runtime.stop();
  }, 10_000);
});

import { spawn } from 'node:child_process';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import { HOOK_RUNNER_SOURCE } from './hookRunnerSource';

const directories: string[] = [];

const runHook = async (
  runnerPath: string,
  spoolPath: string,
  input: string,
): Promise<{ code: number | null; stdout: string; stderr: string }> =>
  new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [
      runnerPath,
      '--owner', 'launchcrush-agent-hook',
      '--source', 'codex',
      '--spool', spoolPath,
    ], { stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += String(chunk); });
    child.stderr.on('data', (chunk) => { stderr += String(chunk); });
    child.once('error', reject);
    child.once('close', (code) => resolve({ code, stdout, stderr }));
    child.stdin.end(input);
  });

const setup = async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'launchcrush-runner-'));
  directories.push(directory);
  const runnerPath = path.join(directory, 'launchcrush-hook.cjs');
  const spoolPath = path.join(directory, 'spool');
  await writeFile(runnerPath, HOOK_RUNNER_SOURCE, 'utf8');
  return { runnerPath, spoolPath };
};

afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) =>
    rm(directory, { recursive: true, force: true })));
});

describe('launchcrush Hook runner', () => {
  it('把完整 stdin payload 原子写为一个 inbox 事件', async () => {
    const { runnerPath, spoolPath } = await setup();
    const payload = {
      session_id: 'session-1',
      hook_event_name: 'UserPromptSubmit',
      prompt: '完整提示词\n第二行',
      tool_input: { command: 'npm test' },
    };

    const result = await runHook(runnerPath, spoolPath, JSON.stringify(payload));
    const files = await readdir(path.join(spoolPath, 'inbox'));
    const stored = JSON.parse(await readFile(
      path.join(spoolPath, 'inbox', files[0]),
      'utf8',
    ));

    expect(result).toEqual({ code: 0, stdout: '', stderr: '' });
    expect(files).toHaveLength(1);
    expect(files[0]).toMatch(/\.json$/);
    expect(stored).toMatchObject({ source: 'codex', payload });
    expect(new Date(stored.receivedAt).toISOString()).toBe(stored.receivedAt);
  });

  it('坏 JSON 不阻塞 Agent 并写入诊断记录', async () => {
    const { runnerPath, spoolPath } = await setup();

    const result = await runHook(runnerPath, spoolPath, '{ broken');
    const diagnostics = await readFile(
      path.join(spoolPath, 'diagnostics/runner-errors.ndjson'),
      'utf8',
    );

    expect(result.code).toBe(0);
    expect(result.stdout).toBe('');
    expect(result.stderr).toBe('');
    expect(diagnostics).toContain('launchcrush-agent-hook');
    expect(diagnostics).toMatch(/Unexpected|Expected/);
  });
});

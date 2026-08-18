import { mkdtemp, mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import type { RawAgentHookEvent } from '../contracts';
import { EventSpool } from './eventSpool';

const directories: string[] = [];

const setup = async (maxHistoryBytes = 20 * 1024 * 1024) => {
  const rootPath = await mkdtemp(path.join(tmpdir(), 'launchcrush-spool-'));
  directories.push(rootPath);
  const inboxPath = path.join(rootPath, 'inbox');
  await mkdir(inboxPath, { recursive: true });
  return {
    rootPath,
    inboxPath,
    spool: new EventSpool({
      rootPath,
      now: () => new Date('2026-08-18T08:00:00.000Z'),
      maxHistoryBytes,
      watchIntervalMs: 25,
    }),
  };
};

const raw = (prompt: string): RawAgentHookEvent => ({
  source: 'codex',
  receivedAt: '2026-08-18T08:00:00.000Z',
  payload: {
    session_id: prompt,
    hook_event_name: 'UserPromptSubmit',
    prompt,
  },
});

afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) =>
    rm(directory, { recursive: true, force: true })));
});

describe('EventSpool', () => {
  it('按文件名消费事件、归档成功事件并隔离坏文件', async () => {
    const { rootPath, inboxPath, spool } = await setup();
    await writeFile(path.join(inboxPath, '002.json'), JSON.stringify(raw('second')));
    await writeFile(path.join(inboxPath, '001.json'), JSON.stringify(raw('first')));
    await writeFile(path.join(inboxPath, '003.json'), '{ broken');
    const prompts: string[] = [];

    await spool.consumePending((event) => { prompts.push(event.prompt!); });

    expect(prompts).toEqual(['first', 'second']);
    await expect(readdir(inboxPath)).resolves.toEqual([]);
    await expect(readdir(path.join(rootPath, 'quarantine'))).resolves.toEqual(['003.json']);
    const history = await readFile(
      path.join(rootPath, 'history/2026-08-18.ndjson'),
      'utf8',
    );
    expect(history.trim().split('\n')).toHaveLength(2);

    await spool.consumePending((event) => { prompts.push(event.prompt!); });
    expect(prompts).toEqual(['first', 'second']);
  });

  it('单日历史超过阈值时轮转后继续写入', async () => {
    const { rootPath, inboxPath, spool } = await setup(20);
    const historyPath = path.join(rootPath, 'history/2026-08-18.ndjson');
    await mkdir(path.dirname(historyPath), { recursive: true });
    await writeFile(historyPath, 'x'.repeat(21));
    await writeFile(path.join(inboxPath, '001.json'), JSON.stringify(raw('rotate')));

    await spool.consumePending(() => undefined);

    expect((await stat(path.join(rootPath, 'history/2026-08-18.1.ndjson'))).size).toBe(21);
    expect(await readFile(historyPath, 'utf8')).toContain('"prompt":"rotate"');
  });

  it('清理超过 30 天的历史文件并保留近期历史', async () => {
    const { rootPath, spool } = await setup();
    const historyDirectory = path.join(rootPath, 'history');
    await mkdir(historyDirectory, { recursive: true });
    await writeFile(path.join(historyDirectory, '2026-07-01.ndjson'), 'old');
    await writeFile(path.join(historyDirectory, '2026-08-17.ndjson'), 'recent');

    await spool.maintainHistory();

    await expect(readdir(historyDirectory)).resolves.toEqual(['2026-08-17.ndjson']);
  });

  it('watch 能消费启动后到达的新事件并可关闭', async () => {
    const { inboxPath, spool } = await setup();
    const received = new Promise<string>((resolve) => {
      spool.watch((event) => resolve(event.prompt!));
    });
    await writeFile(path.join(inboxPath, 'live.json'), JSON.stringify(raw('live')));

    await expect(received).resolves.toBe('live');
    await spool.close();
  });
});

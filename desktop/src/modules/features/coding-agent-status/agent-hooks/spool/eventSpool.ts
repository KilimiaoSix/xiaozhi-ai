import {
  appendFile,
  mkdir,
  readdir,
  readFile,
  rename,
  rm,
  stat,
} from 'node:fs/promises';
import { mkdirSync, watch as watchFs, type FSWatcher } from 'node:fs';
import path from 'node:path';

import type { AgentEvent, AgentSource, RawAgentHookEvent } from '../contracts';
import { normalizeAgentEvent } from '../normalize';

interface EventSpoolOptions {
  rootPath: string;
  now?: () => Date;
  retentionDays?: number;
  maxHistoryBytes?: number;
  watchIntervalMs?: number;
  watchFactory?: (target: string, listener: () => void) => WatcherLike;
}

type EventConsumer = (event: AgentEvent) => void | Promise<void>;

interface WatcherLike {
  close(): void;
  on(event: 'error', listener: (error: Error) => void): unknown;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);

const parseRawEvent = (content: string): RawAgentHookEvent => {
  const value: unknown = JSON.parse(content);
  if (!isRecord(value)
    || !['codex', 'claude-code', 'workbuddy'].includes(String(value.source))
    || typeof value.receivedAt !== 'string'
    || !isRecord(value.payload)) {
    throw new Error('Spool 事件格式无效');
  }
  return {
    source: value.source as AgentSource,
    receivedAt: value.receivedAt,
    payload: value.payload,
  };
};

export class EventSpool {
  private readonly inboxPath: string;
  private readonly historyPath: string;
  private readonly quarantinePath: string;
  private readonly now: () => Date;
  private readonly retentionDays: number;
  private readonly maxHistoryBytes: number;
  private readonly watchIntervalMs: number;
  private watcher?: WatcherLike;
  private interval?: ReturnType<typeof setInterval>;
  private consumeChain: Promise<void> = Promise.resolve();

  constructor(private readonly options: EventSpoolOptions) {
    this.inboxPath = path.join(options.rootPath, 'inbox');
    this.historyPath = path.join(options.rootPath, 'history');
    this.quarantinePath = path.join(options.rootPath, 'quarantine');
    this.now = options.now ?? (() => new Date());
    this.retentionDays = options.retentionDays ?? 30;
    this.maxHistoryBytes = options.maxHistoryBytes ?? 20 * 1024 * 1024;
    this.watchIntervalMs = options.watchIntervalMs ?? 2_000;
  }

  async consumePending(consumer: EventConsumer): Promise<void> {
    this.consumeChain = this.consumeChain.then(() => this.consumePendingNow(consumer));
    await this.consumeChain;
  }

  watch(consumer: EventConsumer): void {
    if (this.watcher) return;
    mkdirSync(this.inboxPath, { recursive: true });
    const schedule = () => { void this.consumePending(consumer); };
    const watchFactory = this.options.watchFactory
      ?? ((target: string, listener: () => void): FSWatcher => watchFs(target, listener));
    try {
      this.watcher = watchFactory(this.inboxPath, schedule);
      this.watcher.on('error', () => {
        this.watcher?.close();
        this.watcher = undefined;
      });
    } catch {
      this.watcher = undefined;
    }
    this.interval = setInterval(schedule, this.watchIntervalMs);
    schedule();
  }

  async close(): Promise<void> {
    this.watcher?.close();
    this.watcher = undefined;
    if (this.interval) clearInterval(this.interval);
    this.interval = undefined;
    await this.consumeChain;
  }

  async maintainHistory(): Promise<void> {
    await mkdir(this.historyPath, { recursive: true });
    const cutoff = this.now().getTime() - this.retentionDays * 24 * 60 * 60 * 1000;
    const files = await readdir(this.historyPath);
    await Promise.all(files.map(async (fileName) => {
      const match = /^(\d{4}-\d{2}-\d{2})(?:\.\d+)?\.ndjson$/.exec(fileName);
      if (!match) return;
      const timestamp = Date.parse(`${match[1]}T00:00:00.000Z`);
      if (!Number.isNaN(timestamp) && timestamp < cutoff) {
        await rm(path.join(this.historyPath, fileName), { force: true });
      }
    }));
  }

  private async consumePendingNow(consumer: EventConsumer): Promise<void> {
    await Promise.all([
      mkdir(this.inboxPath, { recursive: true }),
      mkdir(this.historyPath, { recursive: true }),
      mkdir(this.quarantinePath, { recursive: true }),
    ]);

    const files = (await readdir(this.inboxPath))
      .filter((fileName) => fileName.endsWith('.json'))
      .sort();

    for (const fileName of files) {
      const eventPath = path.join(this.inboxPath, fileName);
      let raw: RawAgentHookEvent;
      try {
        raw = parseRawEvent(await readFile(eventPath, 'utf8'));
      } catch {
        await this.quarantine(eventPath, fileName);
        continue;
      }

      await consumer(normalizeAgentEvent(raw));
      await this.appendHistory(raw);
      await rm(eventPath, { force: true });
    }
  }

  private async appendHistory(raw: RawAgentHookEvent): Promise<void> {
    const day = this.now().toISOString().slice(0, 10);
    const historyFile = path.join(this.historyPath, `${day}.ndjson`);
    try {
      const details = await stat(historyFile);
      if (details.size >= this.maxHistoryBytes) await this.rotate(historyFile, day);
    } catch {
      // The first event of the day creates the file below.
    }
    await appendFile(historyFile, `${JSON.stringify(raw)}\n`, 'utf8');
  }

  private async rotate(historyFile: string, day: string): Promise<void> {
    let index = 1;
    while (true) {
      const candidate = path.join(this.historyPath, `${day}.${index}.ndjson`);
      try {
        await stat(candidate);
        index += 1;
      } catch {
        await rename(historyFile, candidate);
        return;
      }
    }
  }

  private async quarantine(eventPath: string, fileName: string): Promise<void> {
    let target = path.join(this.quarantinePath, fileName);
    try {
      await stat(target);
      target = path.join(this.quarantinePath, `${Date.now()}-${fileName}`);
    } catch {
      // The original filename is available.
    }
    await rename(eventPath, target);
  }
}

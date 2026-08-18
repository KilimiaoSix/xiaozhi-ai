import { describe, expect, it } from 'vitest';

import type { AgentHooksSnapshot } from '../modules/features/coding-agent-status/agent-hooks/runtime';
import {
  AGENT_HOOKS_CHANNELS,
  registerAgentHooksIpc,
  type IpcMainLike,
} from './agentHooksIpc';

describe('registerAgentHooksIpc', () => {
  it('注册固定 channel、校验来源并序列化运行时错误', async () => {
    const handlers = new Map<string, (...args: unknown[]) => unknown>();
    const removed: string[] = [];
    const ipcMain: IpcMainLike = {
      handle: (channel, handler) => { handlers.set(channel, handler); },
      removeHandler: (channel) => { removed.push(channel); },
    };
    const runtime = {
      detect: async () => [{ source: 'codex' }],
      install: async () => { throw new Error('配置不可写'); },
      installAll: async () => [],
      uninstall: async () => ({ ok: true }),
      getSnapshot: () => ({ tasks: [] }),
      subscribe: () => () => undefined,
    };

    const cleanup = registerAgentHooksIpc({
      ipcMain,
      runtime: runtime as never,
      getWindows: () => [],
    });

    expect([...handlers.keys()].sort()).toEqual([
      AGENT_HOOKS_CHANNELS.detect,
      AGENT_HOOKS_CHANNELS.install,
      AGENT_HOOKS_CHANNELS.installAll,
      AGENT_HOOKS_CHANNELS.snapshot,
      AGENT_HOOKS_CHANNELS.uninstall,
    ].sort());
    await expect(handlers.get(AGENT_HOOKS_CHANNELS.detect)!({})).resolves.toEqual({
      ok: true,
      value: [{ source: 'codex' }],
    });
    await expect(
      handlers.get(AGENT_HOOKS_CHANNELS.install)!({}, 'unknown-source'),
    ).resolves.toEqual({ ok: false, error: '未知 Agent 来源：unknown-source' });
    await expect(
      handlers.get(AGENT_HOOKS_CHANNELS.install)!({}, 'codex'),
    ).resolves.toEqual({ ok: false, error: '配置不可写' });

    cleanup();
    expect(removed.sort()).toEqual([...handlers.keys()].sort());
  });

  it('把 runtime 快照广播给所有窗口并在清理时取消订阅', () => {
    let listener: ((snapshot: AgentHooksSnapshot) => void) | undefined;
    let unsubscribed = false;
    const sent: Array<[string, AgentHooksSnapshot]> = [];
    const runtime = {
      detect: async () => [], install: async () => ({}), installAll: async () => [],
      uninstall: async () => ({}), getSnapshot: () => ({ tasks: [] }),
      subscribe: (value: (snapshot: AgentHooksSnapshot) => void) => {
        listener = value;
        return () => { unsubscribed = true; };
      },
    };
    const ipcMain: IpcMainLike = {
      handle: () => undefined,
      removeHandler: () => undefined,
    };
    const cleanup = registerAgentHooksIpc({
      ipcMain,
      runtime: runtime as never,
      getWindows: () => [{
        webContents: {
          isDestroyed: () => false,
          send: (channel: string, snapshot: AgentHooksSnapshot) => {
            sent.push([channel, snapshot]);
          },
        },
      }],
    });
    const snapshot: AgentHooksSnapshot = {
      installations: [],
      primaryTask: null,
      tasks: [],
      actionIntents: [],
      updatedAt: '2026-08-18T08:00:00.000Z',
    };

    listener!(snapshot);

    expect(sent).toEqual([[AGENT_HOOKS_CHANNELS.snapshotChanged, snapshot]]);
    cleanup();
    expect(unsubscribed).toBe(true);
  });
});

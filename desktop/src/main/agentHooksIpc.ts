import type { AgentSource } from '../modules/features/coding-agent-status/agent-hooks/contracts';
import type {
  AgentHooksRuntime,
  AgentHooksSnapshot,
} from '../modules/features/coding-agent-status/agent-hooks/runtime';

export const AGENT_HOOKS_CHANNELS = {
  detect: 'agent-hooks:detect',
  install: 'agent-hooks:install',
  installAll: 'agent-hooks:install-all',
  uninstall: 'agent-hooks:uninstall',
  snapshot: 'agent-hooks:snapshot',
  snapshotChanged: 'agent-hooks:snapshot-changed',
} as const;

export interface IpcMainLike {
  handle(
    channel: string,
    listener: (event: unknown, ...args: unknown[]) => unknown,
  ): void;
  removeHandler(channel: string): void;
}

interface WindowLike {
  webContents: {
    isDestroyed(): boolean;
    send(channel: string, payload: AgentHooksSnapshot): void;
  };
}

interface RegisterAgentHooksIpcOptions {
  ipcMain: IpcMainLike;
  runtime: Pick<
    AgentHooksRuntime,
    'detect' | 'install' | 'installAll' | 'uninstall' | 'getSnapshot' | 'subscribe'
  >;
  getWindows: () => WindowLike[];
}

export type IpcResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: string };

const SOURCES: AgentSource[] = ['codex', 'claude-code', 'workbuddy'];

const safe = async <T>(operation: () => T | Promise<T>): Promise<IpcResult<T>> => {
  try {
    return { ok: true, value: await operation() };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : 'Agent Hook 操作失败',
    };
  }
};

const requireSource = (value: unknown): AgentSource => {
  if (typeof value !== 'string' || !SOURCES.includes(value as AgentSource)) {
    throw new Error(`未知 Agent 来源：${String(value)}`);
  }
  return value as AgentSource;
};

export const registerAgentHooksIpc = (
  options: RegisterAgentHooksIpcOptions,
): (() => void) => {
  const registrations: Array<[
    string,
    (event: unknown, ...args: unknown[]) => unknown,
  ]> = [
    [AGENT_HOOKS_CHANNELS.detect, () => safe(() => options.runtime.detect())],
    [AGENT_HOOKS_CHANNELS.install, (_event, source) =>
      safe(() => options.runtime.install(requireSource(source)))],
    [AGENT_HOOKS_CHANNELS.installAll, () => safe(() => options.runtime.installAll())],
    [AGENT_HOOKS_CHANNELS.uninstall, (_event, source) =>
      safe(() => options.runtime.uninstall(requireSource(source)))],
    [AGENT_HOOKS_CHANNELS.snapshot, () => safe(() => options.runtime.getSnapshot())],
  ];

  for (const [channel, handler] of registrations) {
    options.ipcMain.handle(channel, handler);
  }

  const unsubscribe = options.runtime.subscribe((snapshot) => {
    for (const window of options.getWindows()) {
      if (!window.webContents.isDestroyed()) {
        window.webContents.send(AGENT_HOOKS_CHANNELS.snapshotChanged, snapshot);
      }
    }
  });

  return () => {
    unsubscribe();
    for (const [channel] of registrations) options.ipcMain.removeHandler(channel);
  };
};


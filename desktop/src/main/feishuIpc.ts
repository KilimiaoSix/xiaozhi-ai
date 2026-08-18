import type { LarkCliClient } from '../modules/features/feishu-briefing/larkCli';

export const FEISHU_CHANNELS = {
  status: 'feishu:status',
  briefing: 'feishu:briefing',
} as const;

export interface FeishuIpcMainLike {
  handle(
    channel: string,
    listener: (event: unknown, ...args: unknown[]) => unknown,
  ): void;
  removeHandler(channel: string): void;
}

interface RegisterFeishuIpcOptions {
  ipcMain: FeishuIpcMainLike;
  client: Pick<LarkCliClient, 'getStatus' | 'getBriefing'>;
}

type IpcResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: string };

const safe = async <T>(operation: () => Promise<T>): Promise<IpcResult<T>> => {
  try {
    return { ok: true, value: await operation() };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : '飞书 CLI 操作失败',
    };
  }
};

export const registerFeishuIpc = (options: RegisterFeishuIpcOptions): (() => void) => {
  options.ipcMain.handle(FEISHU_CHANNELS.status, () =>
    safe(() => options.client.getStatus()));
  options.ipcMain.handle(FEISHU_CHANNELS.briefing, () =>
    safe(() => options.client.getBriefing()));

  return () => {
    options.ipcMain.removeHandler(FEISHU_CHANNELS.status);
    options.ipcMain.removeHandler(FEISHU_CHANNELS.briefing);
  };
};


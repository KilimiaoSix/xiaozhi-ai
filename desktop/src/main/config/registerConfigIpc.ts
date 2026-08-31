import { parseAppConfigPatch, type AppConfigResolution } from '../../shared/appConfig';
import type { AppConfigStore } from './appConfigStore';

export const CONFIG_CHANNELS = {
  get: 'config:get',
  update: 'config:update',
} as const;

export interface ConfigIpcMainLike {
  handle(
    channel: string,
    listener: (event: unknown, ...args: unknown[]) => unknown,
  ): void;
  removeHandler(channel: string): void;
}

interface RegisterConfigIpcOptions {
  ipcMain: ConfigIpcMainLike;
  store: Pick<AppConfigStore, 'resolve' | 'update'>;
}

type IpcResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: string };

// 与 feishuIpc 同一套信封：直接抛回 ipcMain.handle 会被 Electron 包成
// "Error invoking remote method ..."，用户看不到真正的原因。
const safe = async <T>(operation: () => Promise<T> | T): Promise<IpcResult<T>> => {
  try {
    return { ok: true, value: await operation() };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : '配置操作失败',
    };
  }
};

export const registerConfigIpc = (options: RegisterConfigIpcOptions): (() => void) => {
  options.ipcMain.handle(
    CONFIG_CHANNELS.get,
    () => safe<AppConfigResolution>(() => options.store.resolve()),
  );
  options.ipcMain.handle(
    CONFIG_CHANNELS.update,
    (_event, patch) => safe<AppConfigResolution>(
      () => options.store.update(parseAppConfigPatch(patch)),
    ),
  );

  return () => {
    for (const channel of Object.values(CONFIG_CHANNELS)) {
      options.ipcMain.removeHandler(channel);
    }
  };
};

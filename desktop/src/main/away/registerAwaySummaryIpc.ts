import {
  AwayGatewayError,
  type AwayIpcResult,
} from '../../modules/features/away-summary/types';
import type { AwaySummaryHttpClient } from './awaySummaryHttpClient';

export const AWAY_CHANNELS = {
  summary: 'away:summary',
} as const;

export interface AwayIpcMainLike {
  handle(
    channel: string,
    listener: (event: unknown, ...args: unknown[]) => unknown,
  ): void;
  removeHandler(channel: string): void;
}

interface RegisterAwaySummaryIpcOptions {
  ipcMain: AwayIpcMainLike;
  client: Pick<AwaySummaryHttpClient, 'getSummary'>;
}

// 与告警管理同理：直接把异常抛回 ipcMain.handle 会被 Electron 包成
// "Error invoking remote method ..."，code/status 全丢。失败以信封回传，
// renderer 侧由 awaySummaryDesktopGateway 还原成 AwayGatewayError。
const safe = async <T>(operation: () => Promise<T>): Promise<AwayIpcResult<T>> => {
  try {
    return { ok: true, value: await operation() };
  } catch (error) {
    if (error instanceof AwayGatewayError) {
      return { ok: false, code: error.code, message: error.message, status: error.status };
    }
    return {
      ok: false,
      code: 'unknown',
      message: error instanceof Error ? error.message : '读取返岗汇总失败',
      status: undefined,
    };
  }
};

export const registerAwaySummaryIpc = (
  options: RegisterAwaySummaryIpcOptions,
): (() => void) => {
  options.ipcMain.handle(
    AWAY_CHANNELS.summary,
    () => safe(() => options.client.getSummary()),
  );

  return () => {
    for (const channel of Object.values(AWAY_CHANNELS)) {
      options.ipcMain.removeHandler(channel);
    }
  };
};

import {
  IncidentGatewayError,
  type IncidentIpcResult,
} from '../../modules/features/incident-assistant/types';
import type { IncidentHttpClient } from './incidentHttpClient';

export const INCIDENT_CHANNELS = {
  list: 'incident:list',
  ack: 'incident:ack',
  diagnose: 'incident:diagnose',
} as const;

export interface IncidentIpcMainLike {
  handle(
    channel: string,
    listener: (event: unknown, ...args: unknown[]) => unknown,
  ): void;
  removeHandler(channel: string): void;
}

interface RegisterIncidentIpcOptions {
  ipcMain: IncidentIpcMainLike;
  client: Pick<IncidentHttpClient, 'list' | 'ack' | 'diagnose'>;
}

// 与番茄钟同理：直接把异常抛回 ipcMain.handle 会被 Electron 包成
// "Error invoking remote method ..."，code/status 全丢。失败以信封回传，
// renderer 侧由 incidentDesktopGateway 还原成 IncidentGatewayError。
const safe = async <T>(operation: () => Promise<T>): Promise<IncidentIpcResult<T>> => {
  try {
    return { ok: true, value: await operation() };
  } catch (error) {
    if (error instanceof IncidentGatewayError) {
      return { ok: false, code: error.code, message: error.message, status: error.status };
    }
    return {
      ok: false,
      code: 'unknown',
      message: error instanceof Error ? error.message : '告警操作失败',
      status: undefined,
    };
  }
};

export const registerIncidentIpc = (
  options: RegisterIncidentIpcOptions,
): (() => void) => {
  options.ipcMain.handle(
    INCIDENT_CHANNELS.list,
    () => safe(() => options.client.list()),
  );
  options.ipcMain.handle(
    INCIDENT_CHANNELS.ack,
    (_event, incidentId) => safe(() => options.client.ack(incidentId as string)),
  );
  options.ipcMain.handle(
    INCIDENT_CHANNELS.diagnose,
    (_event, incidentId) => safe(() => options.client.diagnose(incidentId as string)),
  );

  return () => {
    for (const channel of Object.values(INCIDENT_CHANNELS)) {
      options.ipcMain.removeHandler(channel);
    }
  };
};

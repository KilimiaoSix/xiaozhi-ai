import type { IpcResult } from '../agentHooksIpc';
import type { WellbeingTestResult, WellbeingTestService } from './wellbeingTestService';
import {
  WELLBEING_TEST_KINDS,
  type WellbeingTestKind,
} from '../../modules/features/wellbeing/contracts';

export const WELLBEING_CHANNELS = {
  testEvent: 'wellbeing:test-event',
} as const;

export interface WellbeingIpcMainLike {
  handle(
    channel: string,
    listener: (event: unknown, ...args: unknown[]) => unknown,
  ): void;
  removeHandler(channel: string): void;
}

interface RegisterWellbeingIpcOptions {
  ipcMain: WellbeingIpcMainLike;
  service: Pick<WellbeingTestService, 'sendTest'>;
}

const requireKind = (value: unknown): WellbeingTestKind => {
  if (
    typeof value !== 'string'
    || !WELLBEING_TEST_KINDS.includes(value as WellbeingTestKind)
  ) {
    throw new Error(`未知关怀测试类型：${String(value)}`);
  }
  return value as WellbeingTestKind;
};

const safe = async (
  operation: () => Promise<WellbeingTestResult>,
): Promise<IpcResult<WellbeingTestResult>> => {
  try {
    return { ok: true, value: await operation() };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : '测试提醒发送失败',
    };
  }
};

export const registerWellbeingIpc = (
  options: RegisterWellbeingIpcOptions,
): (() => void) => {
  options.ipcMain.handle(WELLBEING_CHANNELS.testEvent, (_event, kind) =>
    safe(() => options.service.sendTest(requireKind(kind))));
  return () => options.ipcMain.removeHandler(WELLBEING_CHANNELS.testEvent);
};

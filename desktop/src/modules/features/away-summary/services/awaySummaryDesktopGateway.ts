import {
  AwayGatewayError,
  type AwayIpcResult,
  type AwaySummaryResult,
} from '../types';

// main 侧的 AwayGatewayError 过 IPC 只剩一个普通对象，这里按 code/status 原样重建，
// 面板拿到的才是「连接本地 Server 超时」这种能直接上屏的中文原因。
const unwrap = <T>(result: AwayIpcResult<T>): T => {
  if (result.ok) return result.value;
  throw new AwayGatewayError(result.code, result.message, result.status);
};

export const awaySummaryDesktopGateway = {
  getSummary: async (): Promise<AwaySummaryResult> => (
    unwrap(await window.xiaofei.away.getSummary())
  ),
};

export type AwaySummaryDesktopGateway = typeof awaySummaryDesktopGateway;

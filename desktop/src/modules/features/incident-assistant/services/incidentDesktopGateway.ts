import {
  IncidentGatewayError,
  type IncidentAckResult,
  type IncidentDiagnoseResult,
  type IncidentIpcResult,
  type IncidentListResult,
} from '../types';

// main 侧的 IncidentGatewayError 过 IPC 只剩一个普通对象，这里按 code/status 原样重建，
// 面板拿到的才是「连接本地 Server 超时」这种能直接上屏的中文原因。
const unwrap = <T>(result: IncidentIpcResult<T>): T => {
  if (result.ok) return result.value;
  throw new IncidentGatewayError(result.code, result.message, result.status);
};

export const incidentDesktopGateway = {
  list: async (): Promise<IncidentListResult> => (
    unwrap(await window.xiaofei.incident.list())
  ),
  ack: async (incidentId: string): Promise<IncidentAckResult> => (
    unwrap(await window.xiaofei.incident.ack(incidentId))
  ),
  diagnose: async (incidentId: string): Promise<IncidentDiagnoseResult> => (
    unwrap(await window.xiaofei.incident.diagnose(incidentId))
  ),
};

export type IncidentDesktopGateway = typeof incidentDesktopGateway;

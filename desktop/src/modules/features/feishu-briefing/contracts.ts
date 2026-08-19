export type FeishuConnectionState =
  | 'ready'
  | 'configuration_required'
  | 'error';

export interface FeishuConnectionStatus {
  state: FeishuConnectionState;
  message: string;
  openId?: string;
  source: 'server_openapi';
  requiredScopes: string[];
  missingScopes: string[];
}

export interface FeishuMeeting {
  id: string;
  title: string;
  start: string;
  end?: string;
  allDay: boolean;
  url?: string;
}

export interface FeishuTask {
  id: string;
  title: string;
  due?: string;
  allDay: boolean;
  url?: string;
  projectName?: string;
}

export interface FeishuBriefingSnapshot {
  status: FeishuConnectionStatus;
  date: string;
  meetings: FeishuMeeting[];
  tasks: FeishuTask[];
  warnings: string[];
  fetchedAt: string;
}

export type FeishuConnectionState =
  | 'ready'
  | 'cli_missing'
  | 'not_configured'
  | 'needs_auth'
  | 'error';

export interface FeishuCliStatus {
  state: FeishuConnectionState;
  message: string;
  cliVersion?: string;
  userName?: string;
  openId?: string;
  scopes: string[];
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
  status: FeishuCliStatus;
  date: string;
  meetings: FeishuMeeting[];
  tasks: FeishuTask[];
  warnings: string[];
  fetchedAt: string;
}


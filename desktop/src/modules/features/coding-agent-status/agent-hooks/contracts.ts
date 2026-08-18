export type AgentSource = 'codex' | 'claude-code' | 'workbuddy';

export type AgentTaskStatus = 'running' | 'completed' | 'failed' | 'needs_user';

export interface RawAgentHookEvent {
  source: AgentSource;
  receivedAt: string;
  payload: Record<string, unknown>;
}

export interface AgentEvent {
  id: string;
  source: AgentSource;
  sessionId: string;
  turnId?: string;
  eventName: string;
  occurredAt: string;
  cwd?: string;
  transcriptPath?: string;
  prompt?: string;
  finalMessage?: string;
  toolName?: string;
  toolInput?: unknown;
  toolResponse?: unknown;
  notificationType?: string;
  permissionMode?: string;
  error?: string;
  backgroundTaskCount?: number;
}

export interface AgentTaskSnapshot {
  key: string;
  source: AgentSource;
  sessionId: string;
  status: AgentTaskStatus;
  title: string;
  prompt?: string;
  cwd?: string;
  startedAt: string;
  updatedAt: string;
  completedAt?: string;
  error?: string;
  needsUserReason?: string;
}

export interface RobotActionIntent {
  eventId: string;
  taskKey: string;
  action: 'quiet_companion' | 'task_completed' | 'task_failed' | 'needs_user';
  createdAt: string;
  expiresAt: string;
  ttlMs: number;
}

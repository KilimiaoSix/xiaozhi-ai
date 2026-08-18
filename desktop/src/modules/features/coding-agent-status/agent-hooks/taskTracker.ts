import type {
  AgentEvent,
  AgentTaskSnapshot,
  AgentTaskStatus,
  RobotActionIntent,
} from './contracts';

const STATUS_PRIORITY: Record<AgentTaskStatus, number> = {
  needs_user: 4,
  failed: 3,
  running: 2,
  completed: 1,
};

const ACTION_BY_STATUS: Record<AgentTaskStatus, RobotActionIntent['action']> = {
  running: 'quiet_companion',
  completed: 'task_completed',
  failed: 'task_failed',
  needs_user: 'needs_user',
};

const TTL_BY_ACTION: Record<RobotActionIntent['action'], number> = {
  quiet_companion: 15_000,
  task_completed: 30_000,
  task_failed: 30_000,
  needs_user: 600_000,
};

const firstLine = (value: string): string =>
  value.split(/\r?\n/, 1)[0]?.trim() || '未命名任务';

const responseError = (value: unknown): string | undefined => {
  if (!value || typeof value !== 'object') return undefined;
  const record = value as Record<string, unknown>;
  const failed = record.is_error === true
    || record.status === 'failed'
    || (typeof record.exit_code === 'number' && record.exit_code !== 0);
  if (!failed) return undefined;

  for (const key of ['error', 'stderr', 'message']) {
    if (typeof record[key] === 'string' && record[key].length > 0) {
      return record[key];
    }
  }
  return '工具执行失败';
};

const asksForUserDecision = (message: string | undefined): boolean => {
  if (!message) return false;
  const value = message.trim();
  return /(?:请|需要(?:你|您)?|等待(?:你|您)?|麻烦(?:你|您)?).{0,16}(?:确认|选择|决定|决策|批准|授权|同意|允许|回复|输入|提供)/.test(value)
    || /(?:是否|能否|可否|要不要|需不需要).{0,24}[?？]?\s*$/.test(value)
    || /(?:还是|二选一|多选一|选项).{0,24}[?？]\s*$/.test(value)
    || /(?:please\s+(?:confirm|choose|approve|authorize)|do you want me to|would you like me to)/i
      .test(value);
};

interface ApplyOptions {
  recovered?: boolean;
}

export class AgentTaskTracker {
  private readonly tasks = new Map<string, AgentTaskSnapshot>();
  private readonly processedEventIds = new Set<string>();
  private readonly actionIntents: RobotActionIntent[] = [];

  constructor(private readonly now: () => Date = () => new Date()) {}

  apply(event: AgentEvent, options: ApplyOptions = {}): AgentTaskSnapshot {
    const key = `${event.source}:${event.sessionId}`;
    const existing = this.tasks.get(key);
    if (this.processedEventIds.has(event.id) && existing) {
      return { ...existing };
    }
    this.processedEventIds.add(event.id);

    const nextStatus = this.statusFor(event, existing?.status ?? 'running');
    const prompt = event.prompt ?? existing?.prompt;
    const next: AgentTaskSnapshot = {
      key,
      source: event.source,
      sessionId: event.sessionId,
      status: nextStatus,
      title: prompt ? firstLine(prompt) : (existing?.title ?? `${event.source} 任务`),
      ...(prompt ? { prompt } : {}),
      ...(event.cwd ?? existing?.cwd ? { cwd: event.cwd ?? existing?.cwd } : {}),
      startedAt: existing?.startedAt ?? event.occurredAt,
      updatedAt: event.occurredAt,
      ...(nextStatus === 'completed' ? { completedAt: event.occurredAt } : {}),
      ...(nextStatus === 'failed'
        ? { error: event.error ?? responseError(event.toolResponse) ?? event.eventName }
        : {}),
      ...(nextStatus === 'needs_user'
        ? {
            needsUserReason: event.finalMessage
              ?? (event.toolName ? `${event.toolName} 需要用户确认` : 'Agent 正在等待用户介入'),
          }
        : {}),
    };

    this.tasks.set(key, next);
    if (existing?.status !== nextStatus) {
      this.enqueueAction(event, next, options.recovered === true);
    }
    return { ...next };
  }

  list(): AgentTaskSnapshot[] {
    return [...this.tasks.values()]
      .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))
      .map((task) => ({ ...task }));
  }

  primary(): AgentTaskSnapshot | null {
    const task = [...this.tasks.values()].sort((left, right) => {
      const priority = STATUS_PRIORITY[right.status] - STATUS_PRIORITY[left.status];
      return priority || Date.parse(right.updatedAt) - Date.parse(left.updatedAt);
    })[0];
    return task ? { ...task } : null;
  }

  drainActionIntents(): RobotActionIntent[] {
    return this.actionIntents.splice(0).map((intent) => ({ ...intent }));
  }

  restore(tasks: AgentTaskSnapshot[]): void {
    for (const task of tasks) this.tasks.set(task.key, { ...task });
  }

  private statusFor(event: AgentEvent, current: AgentTaskStatus): AgentTaskStatus {
    const responseFailure = responseError(event.toolResponse);
    if (event.eventName === 'StopFailure'
      || event.eventName === 'PostToolUseFailure'
      || event.error
      || responseFailure) {
      return 'failed';
    }

    if (event.eventName === 'PermissionRequest'
      || (event.eventName === 'Notification'
        && event.notificationType === 'permission_prompt')) {
      return 'needs_user';
    }

    if (event.eventName === 'Stop') {
      if ((event.backgroundTaskCount ?? 0) > 0) return 'running';
      if (asksForUserDecision(event.finalMessage)) return 'needs_user';
      return 'completed';
    }

    if (event.eventName === 'SessionEnd') return 'completed';

    if (['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'SessionStart'].includes(event.eventName)) {
      return 'running';
    }

    return current;
  }

  private enqueueAction(
    event: AgentEvent,
    task: AgentTaskSnapshot,
    recovered: boolean,
  ): void {
    if (recovered) return;

    const action = ACTION_BY_STATUS[task.status];
    const ttlMs = TTL_BY_ACTION[action];
    const createdMs = Date.parse(event.occurredAt);
    const safeCreatedMs = Number.isNaN(createdMs) ? this.now().getTime() : createdMs;
    const expiresMs = safeCreatedMs + ttlMs;
    if (expiresMs < this.now().getTime()) return;

    this.actionIntents.push({
      eventId: event.id,
      taskKey: task.key,
      action,
      createdAt: new Date(safeCreatedMs).toISOString(),
      expiresAt: new Date(expiresMs).toISOString(),
      ttlMs,
    });
  }
}

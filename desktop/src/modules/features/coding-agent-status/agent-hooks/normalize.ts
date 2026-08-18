import { createHash } from 'node:crypto';

import type { AgentEvent, RawAgentHookEvent } from './contracts';

const asString = (value: unknown): string | undefined =>
  typeof value === 'string' && value.length > 0 ? value : undefined;

const stableValue = (value: unknown): unknown => {
  if (Array.isArray(value)) {
    return value.map(stableValue);
  }

  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, stableValue(child)]),
    );
  }

  return value;
};

const stableStringify = (value: unknown): string => JSON.stringify(stableValue(value));

const eventTimestamp = (
  payload: Record<string, unknown>,
  receivedAt: string,
): string => {
  const raw = payload.occurred_at ?? payload.timestamp;
  if (typeof raw === 'string' && !Number.isNaN(Date.parse(raw))) {
    return new Date(raw).toISOString();
  }
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return new Date(raw < 10_000_000_000 ? raw * 1000 : raw).toISOString();
  }
  return receivedAt;
};

export const normalizeAgentEvent = (raw: RawAgentHookEvent): AgentEvent => {
  const { payload } = raw;
  const cwd = asString(payload.cwd);
  const sessionId = asString(payload.session_id)
    ?? `${raw.source}:${cwd ?? 'unknown'}:${raw.receivedAt}`;
  const turnId = asString(payload.turn_id) ?? asString(payload.prompt_id);
  const eventName = asString(payload.hook_event_name)
    ?? asString(payload.event_name)
    ?? 'Unknown';

  const identity = stableStringify({
    source: raw.source,
    sessionId,
    turnId,
    eventName,
    toolUseId: payload.tool_use_id ?? payload.call_id,
    payload,
  });

  const backgroundTasks = payload.background_tasks;

  return {
    id: createHash('sha256').update(identity).digest('hex'),
    source: raw.source,
    sessionId,
    ...(turnId ? { turnId } : {}),
    eventName,
    occurredAt: eventTimestamp(payload, raw.receivedAt),
    ...(cwd ? { cwd } : {}),
    ...(asString(payload.transcript_path)
      ? { transcriptPath: asString(payload.transcript_path) }
      : {}),
    ...(asString(payload.prompt) ? { prompt: asString(payload.prompt) } : {}),
    ...(asString(payload.last_assistant_message)
      ? { finalMessage: asString(payload.last_assistant_message) }
      : {}),
    ...(asString(payload.tool_name) ? { toolName: asString(payload.tool_name) } : {}),
    ...('tool_input' in payload ? { toolInput: payload.tool_input } : {}),
    ...('tool_response' in payload ? { toolResponse: payload.tool_response } : {}),
    ...(asString(payload.notification_type)
      ? { notificationType: asString(payload.notification_type) }
      : {}),
    ...(asString(payload.error) ? { error: asString(payload.error) } : {}),
    ...(Array.isArray(backgroundTasks)
      ? { backgroundTaskCount: backgroundTasks.length }
      : {}),
  };
};


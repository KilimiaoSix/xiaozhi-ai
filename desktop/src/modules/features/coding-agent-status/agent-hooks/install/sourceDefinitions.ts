import type { AgentHookSourceDefinition } from './types';

const TOOL_EVENTS = [
  'PreToolUse',
  'PermissionRequest',
  'PostToolUse',
  'PostToolUseFailure',
];

export const SOURCE_DEFINITIONS = {
  codex: {
    source: 'codex',
    commandNames: ['codex'],
    events: [
      'SessionStart',
      'UserPromptSubmit',
      'PreToolUse',
      'PermissionRequest',
      'PostToolUse',
      'Stop',
      'SessionEnd',
    ],
    toolEvents: TOOL_EVENTS,
  },
  'claude-code': {
    source: 'claude-code',
    commandNames: ['claude'],
    events: [
      'SessionStart',
      'UserPromptSubmit',
      'PreToolUse',
      'PermissionRequest',
      'PostToolUse',
      'PostToolUseFailure',
      'Notification',
      'Stop',
      'StopFailure',
      'SessionEnd',
    ],
    toolEvents: TOOL_EVENTS,
  },
  workbuddy: {
    source: 'workbuddy',
    commandNames: ['workbuddy', 'codebuddy'],
    events: [
      'SessionStart',
      'UserPromptSubmit',
      'PreToolUse',
      'PermissionRequest',
      'PostToolUse',
      'Notification',
      'Stop',
      'SessionEnd',
    ],
    toolEvents: TOOL_EVENTS,
  },
} satisfies Record<string, AgentHookSourceDefinition>;


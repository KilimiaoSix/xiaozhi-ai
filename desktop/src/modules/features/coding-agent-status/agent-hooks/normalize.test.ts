import { describe, expect, it } from 'vitest';

import type { AgentSource, RawAgentHookEvent } from './contracts';
import { normalizeAgentEvent } from './normalize';

const createRaw = (
  source: AgentSource,
  payload: Record<string, unknown>,
): RawAgentHookEvent => ({
  source,
  receivedAt: '2026-08-18T08:00:00.000Z',
  payload,
});

describe('normalizeAgentEvent', () => {
  it('保留 Codex 的完整任务内容并生成稳定事件 ID', () => {
    const raw = createRaw('codex', {
      session_id: 'codex-1',
      turn_id: 'turn-1',
      hook_event_name: 'UserPromptSubmit',
      cwd: '/repo',
      transcript_path: '/tmp/codex.jsonl',
      prompt: '完整实现登录模块并运行全部测试\n不要省略任何步骤',
    });

    const first = normalizeAgentEvent(raw);
    const second = normalizeAgentEvent(raw);

    expect(first).toMatchObject({
      source: 'codex',
      sessionId: 'codex-1',
      turnId: 'turn-1',
      eventName: 'UserPromptSubmit',
      cwd: '/repo',
      transcriptPath: '/tmp/codex.jsonl',
      prompt: '完整实现登录模块并运行全部测试\n不要省略任何步骤',
    });
    expect(first.id).toBe(second.id);
  });

  it('保留 Claude Code 的失败详情和完整工具载荷', () => {
    const event = normalizeAgentEvent(createRaw('claude-code', {
      session_id: 'claude-1',
      prompt_id: 'prompt-1',
      hook_event_name: 'PostToolUseFailure',
      tool_name: 'Bash',
      tool_input: { command: 'npm test', description: '运行测试' },
      tool_response: { is_error: true, stderr: '2 tests failed' },
      error: '命令执行失败',
    }));

    expect(event).toMatchObject({
      source: 'claude-code',
      sessionId: 'claude-1',
      turnId: 'prompt-1',
      eventName: 'PostToolUseFailure',
      toolName: 'Bash',
      toolInput: { command: 'npm test', description: '运行测试' },
      toolResponse: { is_error: true, stderr: '2 tests failed' },
      error: '命令执行失败',
    });
  });

  it('识别 WorkBuddy 等待通知并保留最终回复', () => {
    const event = normalizeAgentEvent(createRaw('workbuddy', {
      session_id: 'workbuddy-1',
      hook_event_name: 'Notification',
      notification_type: 'idle_prompt',
      last_assistant_message: '任务已整理好，请确认是否发送？',
      background_tasks: [{ id: 'background-1' }],
    }));

    expect(event).toMatchObject({
      source: 'workbuddy',
      sessionId: 'workbuddy-1',
      eventName: 'Notification',
      notificationType: 'idle_prompt',
      finalMessage: '任务已整理好，请确认是否发送？',
      backgroundTaskCount: 1,
    });
  });

  it('保留自动授权模式供状态规则使用', () => {
    const event = normalizeAgentEvent(createRaw('workbuddy', {
      session_id: 'workbuddy-auto-approval',
      hook_event_name: 'PermissionRequest',
      permission_mode: 'bypassPermissions',
      tool_name: 'Bash',
    }));

    expect(event).toMatchObject({
      source: 'workbuddy',
      eventName: 'PermissionRequest',
      permissionMode: 'bypassPermissions',
      toolName: 'Bash',
    });
  });

  it('缺少 session_id 时生成可关联的临时会话 ID', () => {
    const event = normalizeAgentEvent(createRaw('codex', {
      hook_event_name: 'SessionStart',
      cwd: '/repo-without-session',
    }));

    expect(event.sessionId).toBe(
      'codex:/repo-without-session:2026-08-18T08:00:00.000Z',
    );
  });
});

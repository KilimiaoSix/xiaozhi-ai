import { describe, expect, it } from 'vitest';

import type { AgentEvent } from './contracts';
import { AgentTaskTracker } from './taskTracker';

let sequence = 0;

const event = (
  eventName: string,
  overrides: Partial<AgentEvent> = {},
): AgentEvent => ({
  id: `event-${sequence += 1}`,
  source: 'codex',
  sessionId: 'session-1',
  eventName,
  occurredAt: '2026-08-18T08:00:00.000Z',
  ...overrides,
});

const createTracker = () =>
  new AgentTaskTracker(() => new Date('2026-08-18T08:05:00.000Z'));

describe('AgentTaskTracker', () => {
  it('把任务从运行推进到等待用户再推进到完成', () => {
    const tracker = createTracker();

    tracker.apply(event('UserPromptSubmit', {
      source: 'claude-code',
      prompt: '修复登录失败\n运行全部回归测试',
      cwd: '/repo',
    }));
    expect(tracker.primary()).toMatchObject({
      status: 'running',
      title: '修复登录失败',
      prompt: '修复登录失败\n运行全部回归测试',
      cwd: '/repo',
    });

    tracker.apply(event('Notification', {
      source: 'claude-code',
      notificationType: 'permission_prompt',
      toolName: 'Bash',
    }));
    expect(tracker.primary()).toMatchObject({
      status: 'needs_user',
      needsUserReason: 'Bash 需要用户确认',
    });

    tracker.apply(event('Stop', {
      source: 'claude-code',
      finalMessage: '修复和测试均已完成。',
    }));
    expect(tracker.primary()).toMatchObject({ status: 'completed' });
  });

  it('Codex 权限请求等待用户，工具继续后恢复运行', () => {
    const tracker = createTracker();

    tracker.apply(event('UserPromptSubmit', {
      source: 'codex',
      prompt: '运行 Bash 任务',
    }));
    tracker.apply(event('PermissionRequest', {
      source: 'codex',
      toolName: 'Bash',
    }));

    expect(tracker.primary()).toMatchObject({
      source: 'codex',
      status: 'needs_user',
      needsUserReason: 'Bash 需要用户确认',
    });

    tracker.apply(event('PostToolUse', {
      source: 'codex',
      toolName: 'Bash',
    }));
    expect(tracker.primary()).toMatchObject({
      source: 'codex',
      status: 'running',
    });
  });

  it('Stop 在后台任务运行或最终回复提问时不会误报完成', () => {
    const runningTracker = createTracker();
    runningTracker.apply(event('UserPromptSubmit', { prompt: '构建项目' }));
    runningTracker.apply(event('Stop', { backgroundTaskCount: 1 }));
    expect(runningTracker.primary()?.status).toBe('running');

    const waitingTracker = createTracker();
    waitingTracker.apply(event('UserPromptSubmit', {
      sessionId: 'question-session',
      prompt: '部署项目',
    }));
    waitingTracker.apply(event('Stop', {
      sessionId: 'question-session',
      finalMessage: '部署前需要你确认环境，是否继续？',
    }));
    expect(waitingTracker.primary()).toMatchObject({
      status: 'needs_user',
      needsUserReason: '部署前需要你确认环境，是否继续？',
    });
  });

  it('WorkBuddy 的普通问候结束后保持完成', () => {
    const tracker = createTracker();
    tracker.apply(event('UserPromptSubmit', {
      source: 'workbuddy',
      sessionId: 'workbuddy-greeting',
      prompt: '打个招呼',
    }));

    tracker.apply(event('Stop', {
      source: 'workbuddy',
      sessionId: 'workbuddy-greeting',
      finalMessage: '齐哥好。有什么需要帮忙的？',
    }));

    expect(tracker.primary()).toMatchObject({ status: 'completed' });
  });

  it('WorkBuddy 完成后的通用 idle_prompt 不会覆盖完成状态', () => {
    const tracker = createTracker();
    tracker.apply(event('UserPromptSubmit', {
      source: 'workbuddy',
      sessionId: 'workbuddy-idle',
      prompt: '没有就打个招呼',
    }));
    tracker.apply(event('Stop', {
      source: 'workbuddy',
      sessionId: 'workbuddy-idle',
      finalMessage: '齐哥好，随时待命。',
    }));
    tracker.apply(event('Notification', {
      source: 'workbuddy',
      sessionId: 'workbuddy-idle',
      notificationType: 'idle_prompt',
    }));

    expect(tracker.primary()).toMatchObject({ status: 'completed' });
  });

  it('失败事件进入 failed，后续工具执行可恢复 running', () => {
    const tracker = createTracker();
    tracker.apply(event('UserPromptSubmit', { prompt: '运行测试' }));
    tracker.apply(event('PostToolUseFailure', {
      error: '测试命令退出码为 1',
    }));
    expect(tracker.primary()).toMatchObject({
      status: 'failed',
      error: '测试命令退出码为 1',
    });

    tracker.apply(event('PreToolUse', { toolName: 'Bash' }));
    expect(tracker.primary()).toMatchObject({ status: 'running' });
    expect(tracker.primary()).not.toHaveProperty('error');
  });

  it('从工具响应中的明确错误识别失败', () => {
    const tracker = createTracker();
    tracker.apply(event('UserPromptSubmit', { prompt: '打包应用' }));
    tracker.apply(event('PostToolUse', {
      toolResponse: { is_error: true, stderr: 'package failed' },
    }));

    expect(tracker.primary()).toMatchObject({
      status: 'failed',
      error: 'package failed',
    });
  });

  it('按等待、失败、运行、完成的顺序选择主任务', () => {
    const tracker = createTracker();
    tracker.apply(event('UserPromptSubmit', {
      source: 'codex', sessionId: 'completed', prompt: '已完成任务',
    }));
    tracker.apply(event('Stop', { source: 'codex', sessionId: 'completed' }));
    tracker.apply(event('UserPromptSubmit', {
      source: 'claude-code', sessionId: 'running', prompt: '运行任务',
    }));
    tracker.apply(event('StopFailure', {
      source: 'workbuddy', sessionId: 'failed', error: '失败任务',
    }));
    tracker.apply(event('Notification', {
      source: 'claude-code', sessionId: 'waiting', toolName: 'Bash',
      notificationType: 'permission_prompt',
    }));

    expect(tracker.primary()).toMatchObject({
      key: 'claude-code:waiting',
      status: 'needs_user',
    });
    expect(tracker.list()).toHaveLength(4);
  });

  it('生成有固定 TTL 的预设动作并忽略重复或恢复事件', () => {
    const tracker = createTracker();
    const start = event('UserPromptSubmit', {
      id: 'stable-start',
      source: 'claude-code',
      occurredAt: '2026-08-18T08:05:00.000Z',
      prompt: '开始任务',
    });

    tracker.apply(start);
    tracker.apply(start);
    tracker.apply(event('Notification', {
      source: 'claude-code',
      occurredAt: '2026-08-18T08:05:00.000Z',
      toolName: 'Bash',
      notificationType: 'permission_prompt',
    }));

    expect(tracker.drainActionIntents()).toEqual([
      expect.objectContaining({ action: 'quiet_companion', ttlMs: 15_000 }),
      expect.objectContaining({ action: 'needs_user', ttlMs: 600_000 }),
    ]);
    expect(tracker.drainActionIntents()).toEqual([]);

    tracker.apply(event('Stop', {
      sessionId: 'recovered-session',
      occurredAt: '2026-08-17T08:00:00.000Z',
    }), { recovered: true });
    expect(tracker.drainActionIntents()).toEqual([]);
  });
});

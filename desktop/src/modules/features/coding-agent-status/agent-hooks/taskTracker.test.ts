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
  it('Codex 标题读取 My request 后的真实请求', () => {
    const tracker = createTracker();

    tracker.apply(event('UserPromptSubmit', {
      source: 'codex',
      prompt: [
        '# Context from my IDE setup:',
        '',
        '## Open tabs:',
        '- taskTracker.ts',
        '',
        '## My request:',
        '',
        '修正 Codex 标题提取',
        '并补充回归测试',
      ].join('\n'),
    }));

    expect(tracker.primary()).toMatchObject({
      source: 'codex',
      title: '修正 Codex 标题提取',
    });
  });

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

  it('Codex 权限请求不直接视为等待用户', () => {
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
      status: 'running',
    });
    expect(tracker.primary()).not.toHaveProperty('needsUserReason');

    tracker.apply(event('Notification', {
      source: 'codex',
      notificationType: 'permission_prompt',
      toolName: 'Bash',
    }));
    expect(tracker.primary()).toMatchObject({
      source: 'codex',
      status: 'running',
    });
  });

  it('非 Codex 的人工权限请求仍等待用户', () => {
    const tracker = createTracker();

    tracker.apply(event('PermissionRequest', {
      source: 'claude-code',
      permissionMode: 'default',
      toolName: 'Bash',
    }));

    expect(tracker.primary()).toMatchObject({
      source: 'claude-code',
      status: 'needs_user',
      needsUserReason: 'Bash 需要用户确认',
    });
  });

  it('自动授权模式下的权限事件保持运行', () => {
    const tracker = createTracker();

    tracker.apply(event('PermissionRequest', {
      source: 'workbuddy',
      permissionMode: 'bypassPermissions',
      toolName: 'Bash',
    }));

    expect(tracker.primary()).toMatchObject({
      source: 'workbuddy',
      status: 'running',
    });
    expect(tracker.primary()).not.toHaveProperty('needsUserReason');

    tracker.apply(event('Notification', {
      source: 'workbuddy',
      permissionMode: 'bypassPermissions',
      notificationType: 'permission_prompt',
      toolName: 'Bash',
    }));
    expect(tracker.primary()).toMatchObject({
      source: 'workbuddy',
      status: 'running',
    });

    tracker.apply(event('PostToolUse', {
      source: 'workbuddy',
      toolName: 'Bash',
    }));
    expect(tracker.primary()).toMatchObject({
      source: 'workbuddy',
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

  it('Codex 的普通后续邀请不会误报等待用户', () => {
    const tracker = createTracker();
    tracker.apply(event('UserPromptSubmit', {
      source: 'codex',
      sessionId: 'codex-greeting',
      prompt: '你好',
    }));

    tracker.apply(event('Stop', {
      source: 'codex',
      sessionId: 'codex-greeting',
      finalMessage: '你好。你想从 taskTracker.ts 开始看，还是有别的事情要处理？',
    }));

    expect(tracker.primary()).toMatchObject({ status: 'completed' });
    expect(tracker.primary()).not.toHaveProperty('needsUserReason');
  });

  it('完成总结中提到等待批准不会误报等待用户', () => {
    const tracker = createTracker();
    tracker.apply(event('UserPromptSubmit', {
      source: 'codex',
      sessionId: 'codex-approval-summary',
      prompt: '修复权限误报',
    }));

    tracker.apply(event('Stop', {
      source: 'codex',
      sessionId: 'codex-approval-summary',
      finalMessage: '只有 Codex 侧栏真实显示“等待批准”时才进入 needs_user，真正停在侧栏等待批准的操作仍会提醒。',
    }));

    expect(tracker.primary()).toMatchObject({ status: 'completed' });
    expect(tracker.primary()).not.toHaveProperty('needsUserReason');
  });

  it('恢复状态时纠正旧规则产生的 Codex 误判', () => {
    const tracker = createTracker();
    tracker.restore([{
      key: 'codex:historical-false-positive',
      source: 'codex',
      sessionId: 'historical-false-positive',
      status: 'needs_user',
      title: '修复权限误报',
      startedAt: '2026-08-18T08:00:00.000Z',
      updatedAt: '2026-08-18T08:00:30.000Z',
      needsUserReason: '真正停在侧栏等待批准的操作仍会提醒。',
    }]);

    expect(tracker.primary()).toMatchObject({ status: 'completed' });
    expect(tracker.primary()).not.toHaveProperty('needsUserReason');
  });

  it('恢复状态时保留明确等待确认的 Codex 任务', () => {
    const tracker = createTracker();
    tracker.restore([{
      key: 'codex:historical-user-decision',
      source: 'codex',
      sessionId: 'historical-user-decision',
      status: 'needs_user',
      title: '部署项目',
      startedAt: '2026-08-18T08:00:00.000Z',
      updatedAt: '2026-08-18T08:00:30.000Z',
      needsUserReason: '部署前需要你确认环境，是否继续？',
    }]);

    expect(tracker.primary()).toMatchObject({
      status: 'needs_user',
      needsUserReason: '部署前需要你确认环境，是否继续？',
    });
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

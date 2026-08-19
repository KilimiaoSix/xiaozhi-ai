import { describe, expect, it } from 'vitest';

import { mapIntentToPush } from './robotIntentMapper';
import type { AgentTaskSnapshot, RobotActionIntent } from '../../modules/features/coding-agent-status/agent-hooks/contracts';

const DEVICE = 'dc:da:0c:26:9a:60';

const intent = (action: RobotActionIntent['action']): RobotActionIntent => ({
  eventId: 'evt-1',
  taskKey: 'claude-code:sess-1',
  action,
  createdAt: '2026-08-18T13:00:00.000Z',
  expiresAt: '2026-08-18T13:00:30.000Z',
  ttlMs: 30_000,
});

const task = (over: Partial<AgentTaskSnapshot> = {}): AgentTaskSnapshot => ({
  key: 'claude-code:sess-1',
  source: 'claude-code',
  sessionId: 'sess-1',
  status: 'completed',
  title: '补接口参数校验',
  startedAt: '2026-08-18T12:59:00.000Z',
  updatedAt: '2026-08-18T13:00:00.000Z',
  ...over,
});

describe('mapIntentToPush', () => {
  it('任务完成时抬头点头并开口播报', () => {
    const push = mapIntentToPush(DEVICE, intent('task_completed'), task());

    expect(push).toMatchObject({
      device_id: DEVICE,
      emotion: 'happy',
      action: 'nod',
      speak: true,
    });
    expect(push.text).toContain('补接口参数校验');
    // 终态播完自动收场，不长期占着屏幕
    expect(push.restore_after).toBeGreaterThan(0);
  });

  it('任务失败时低头，不摇头也不擅自重试', () => {
    const push = mapIntentToPush(
      DEVICE,
      intent('task_failed'),
      task({ status: 'failed', error: 'pytest 收集阶段报 ImportError' }),
    );

    expect(push.emotion).toBe('sad');
    expect(push.speak).toBe(true);
    expect(push.text).toContain('补接口参数校验');
  });

  it('失败详情不进播报：机器人不该把报错原文念出来', () => {
    // 真机上出现过念「Exit code 1 Traceback (m…」的情况。
    // AGENTS.md 硬件边界写明富信息一律落到电脑屏幕，报错属于桌面端卡片。
    const push = mapIntentToPush(
      DEVICE,
      intent('task_failed'),
      task({
        status: 'failed',
        error: 'Exit code 1 Traceback (most recent call last):\n  File "x.py"',
      }),
    );

    expect(push.text).not.toMatch(/Traceback|Exit code|File "/);
    expect(push.text).toBe('补接口参数校验 失败了');
  });

  it('等待用户介入时歪头提醒，且不自动复位画面', () => {
    const push = mapIntentToPush(
      DEVICE,
      intent('needs_user'),
      task({ status: 'needs_user', needsUserReason: '要执行 rm，需要授权' }),
    );

    expect(push.emotion).toBe('confused');
    expect(push.speak).toBe(true);
    expect(push.text).toContain('要执行 rm');
    // needs_user 是唯一必须持续可见的态，只应由后续终态清场
    expect(push.restore_after).toBeUndefined();
  });

  it('运行中只做安静陪伴：不出声、不响提示音', () => {
    const push = mapIntentToPush(
      DEVICE,
      intent('quiet_companion'),
      task({ status: 'running' }),
    );

    expect(push.speak).toBe(false);
    expect(push.silent).toBe(true);
  });

  it('缺少 error 与 needsUserReason 时仍给出可播报的文案', () => {
    const failed = mapIntentToPush(DEVICE, intent('task_failed'), task({ status: 'failed' }));
    const needs = mapIntentToPush(DEVICE, intent('needs_user'), task({ status: 'needs_user' }));

    expect(failed.text.length).toBeGreaterThan(0);
    expect(needs.text.length).toBeGreaterThan(0);
  });

  it('拿不到任务快照时退化成通用文案而不是崩掉', () => {
    const push = mapIntentToPush(DEVICE, intent('task_completed'), undefined);

    expect(push.device_id).toBe(DEVICE);
    expect(push.text.length).toBeGreaterThan(0);
  });

  it('超长标题会被截断，避免把整段提示词读出来', () => {
    const long = '把'.repeat(200);
    const push = mapIntentToPush(DEVICE, intent('task_completed'), task({ title: long }));

    expect(push.text.length).toBeLessThan(80);
  });

  it('文案只用标题，不编造改动文件数或测试数', () => {
    const push = mapIntentToPush(DEVICE, intent('task_completed'), task());

    expect(push.text).not.toMatch(/\d+\s*个文件|\d+\s*项测试/);
  });
});

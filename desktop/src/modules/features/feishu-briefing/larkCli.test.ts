import { describe, expect, it } from 'vitest';

import {
  LarkCliClient,
  parseMeetings,
  parseTasks,
  type LarkCliCommandResult,
  type LarkCliCommandRunner,
} from './larkCli';

class QueueRunner implements LarkCliCommandRunner {
  readonly calls: string[][] = [];

  constructor(private readonly results: LarkCliCommandResult[]) {}

  async run(args: string[]): Promise<LarkCliCommandResult> {
    this.calls.push(args);
    const result = this.results.shift();
    if (!result) throw new Error(`缺少命令结果：${args.join(' ')}`);
    return result;
  }
}

const output = (value: unknown): LarkCliCommandResult => ({
  stdout: JSON.stringify(value),
  stderr: '',
});

const readyStatus = output({
  identity: 'user',
  verified: true,
  identities: {
    user: {
      available: true,
      verified: true,
      userName: '小飞用户',
      openId: 'ou_user',
      scope: 'task:task:read calendar:calendar:readonly',
    },
  },
});

describe('LarkCliClient', () => {
  it('读取用户登录态并报告缺失的最小权限', async () => {
    const runner = new QueueRunner([
      { stdout: 'lark-cli version v1.0.68\n', stderr: '' },
      output({
        identity: 'user',
        verified: true,
        identities: {
          user: {
            available: true,
            verified: true,
            userName: '小飞用户',
            openId: 'ou_user',
            scope: 'task:task:read',
          },
        },
      }),
    ]);

    await expect(new LarkCliClient(runner).getStatus()).resolves.toMatchObject({
      state: 'ready',
      cliVersion: 'v1.0.68',
      userName: '小飞用户',
      missingScopes: ['calendar:calendar:readonly'],
    });
  });

  it('并行读取今日日程与未完成任务并标准化结果', async () => {
    const runner = new QueueRunner([
      { stdout: 'lark-cli version v1.0.68\n', stderr: '' },
      readyStatus,
      output({
        ok: true,
        identity: 'user',
        data: {
          items: [{
            event_id: 'evt_1',
            summary: '产品周会',
            start: { timestamp: '1787011200', timezone: 'Asia/Shanghai' },
            end: { timestamp: '1787014800', timezone: 'Asia/Shanghai' },
          }],
        },
      }),
      output({
        ok: true,
        identity: 'user',
        data: {
          items: [{
            guid: 'task_1',
            summary: '整理演示材料',
            due: { timestamp: '1787068800000', is_all_day: false },
            url: 'https://example.com/task_1',
            tasklists: [{ name: '桌面精灵' }],
          }],
        },
      }),
    ]);

    const snapshot = await new LarkCliClient(runner).getBriefing(
      new Date('2026-08-18T08:00:00+08:00'),
    );

    expect(snapshot.date).toBe('2026-08-18');
    expect(snapshot.meetings).toHaveLength(1);
    expect(snapshot.tasks).toEqual([
      expect.objectContaining({ id: 'task_1', title: '整理演示材料', projectName: '桌面精灵' }),
    ]);
    expect(runner.calls[2]).toContain('2026-08-18');
    expect(runner.calls[3]).toContain('--complete=false');
  });

  it('日历缺权时仍返回任务并给出可操作警告', async () => {
    const runner = new QueueRunner([
      { stdout: 'lark-cli version v1.0.68\n', stderr: '' },
      readyStatus,
      output({
        ok: false,
        error: {
          subtype: 'missing_scope',
          message: 'unauthorized',
          missing_scopes: ['calendar:calendar:readonly'],
        },
      }),
      output({ ok: true, data: { has_more: false, items: null } }),
    ]);

    const snapshot = await new LarkCliClient(runner).getBriefing(
      new Date('2026-08-18T08:00:00+08:00'),
    );

    expect(snapshot.tasks).toEqual([]);
    expect(snapshot.warnings).toEqual([
      '今日日程缺少飞书权限：calendar:calendar:readonly',
    ]);
  });
});

describe('飞书字段解析', () => {
  it('兼容全天日期、Unix 时间戳和空 items', () => {
    expect(parseMeetings({ items: null })).toEqual([]);
    expect(parseMeetings({
      items: [{ id: 'event', title: '全天活动', start_time: { date: '2026-08-18' } }],
    })[0]).toMatchObject({ start: '2026-08-18', allDay: true });
    expect(parseTasks({
      items: [{ id: 'task', title: '按时完成', due_time: '1787068800' }],
    })[0]?.due).toMatch(/^2026-/);
  });
});


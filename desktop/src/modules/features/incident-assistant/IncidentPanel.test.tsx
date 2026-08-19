/** @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { IncidentPanel } from './IncidentPanel';
import type { IncidentListResult, IncidentSummary } from './types';

const incident = (overrides: Partial<IncidentSummary> = {}): IncidentSummary => ({
  id: 'demo-1',
  source: 'incident',
  service: 'demo-api',
  severity: 'P1',
  title: '接口错误率升高',
  message: '支付回调错误率 12%',
  state: 'firing',
  repeatCount: 3,
  firstSeenAt: '2026-08-19T10:00:00',
  lastSeenAt: '2026-08-19T10:05:00',
  recoveredAt: null,
  announced: true,
  acknowledged: false,
  simulated: false,
  diagnosis: null,
  timeline: [
    { at: '2026-08-19T10:00:00', event: 'received', detail: '首次收到告警' },
    { at: '2026-08-19T10:00:01', event: 'announced', detail: '线上告警：接口错误率升高' },
  ],
  ...overrides,
});

const listResult = (incidents: IncidentSummary[]): IncidentListResult => ({
  date: '2026-08-19',
  incidents,
});

const gatewayMocks = vi.hoisted(() => ({
  list: vi.fn(),
  ack: vi.fn(),
  diagnose: vi.fn(),
}));

vi.mock('./services/incidentDesktopGateway', () => ({
  incidentDesktopGateway: gatewayMocks,
}));

const flush = async (): Promise<void> => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};

const findButton = (container: HTMLElement, selector: string, index = 0): HTMLButtonElement => {
  const button = container.querySelectorAll<HTMLButtonElement>(selector)[index];
  if (!button) throw new Error(`找不到按钮 ${selector}[${index}]`);
  return button;
};

describe('IncidentPanel', () => {
  let container: HTMLDivElement;
  let root: Root;

  const mount = async (): Promise<void> => {
    await act(async () => {
      root.render(<IncidentPanel />);
      await flush();
    });
  };

  beforeEach(() => {
    gatewayMocks.list.mockReset();
    gatewayMocks.ack.mockReset();
    gatewayMocks.diagnose.mockReset();
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('按严重度再按状态排序，P0/P1 的 firing 行高亮', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([
      incident({ id: 'a', severity: 'P1', state: 'firing', title: 'P1燃烧' }),
      incident({ id: 'b', severity: 'P0', state: 'recovered', title: 'P0已恢复', recoveredAt: '2026-08-19T09:00:00' }),
      incident({ id: 'c', severity: 'P0', state: 'firing', title: 'P0燃烧' }),
      incident({ id: 'd', severity: 'P2', state: 'firing', title: 'P2燃烧' }),
    ]));

    await mount();

    const titles = [...container.querySelectorAll('.incident-title')].map(
      (node) => node.textContent,
    );
    expect(titles).toEqual(['P0燃烧', 'P0已恢复', 'P1燃烧', 'P2燃烧']);

    const rows = [...container.querySelectorAll('.incident-row')];
    expect(rows[0].className).toContain('is-critical'); // P0 firing
    expect(rows[1].className).not.toContain('is-critical'); // P0 recovered 不再高亮
    expect(rows[2].className).toContain('is-critical'); // P1 firing
    expect(rows[3].className).not.toContain('is-critical'); // P2 firing 不高亮
  });

  it('模拟告警带明显「模拟」标识', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([incident({ simulated: true })]));

    await mount();

    expect(container.querySelector('.incident-simulated')?.textContent).toBe('模拟');
  });

  it('展开行显示时间线与诊断结论', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([
      incident({
        diagnosis: { state: 'done', summary: '根因是上游超时', finishedAt: '2026-08-19T10:20:00' },
      }),
    ]));

    await mount();
    await act(async () => {
      findButton(container, '.incident-row-head').click();
      await flush();
    });

    const detail = container.querySelector('.incident-detail');
    expect(detail?.textContent).toContain('根因是上游超时');
    expect(detail?.textContent).toContain('首次收到告警');
    expect(detail?.textContent).toContain('received');
  });

  it('诊断失败时展开区显示失败原因', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([
      incident({
        diagnosis: { state: 'failed', summary: '诊断超时，已停在 300 秒', finishedAt: '2026-08-19T10:20:00' },
      }),
    ]));

    await mount();
    await act(async () => {
      findButton(container, '.incident-row-head').click();
      await flush();
    });

    expect(container.querySelector('.incident-detail')?.textContent).toContain(
      '诊断超时，已停在 300 秒',
    );
  });

  it('点击标记已处理后调用 ack 并立即刷新', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([incident()]));
    gatewayMocks.ack.mockResolvedValue({ acknowledged: true });

    await mount();
    await act(async () => {
      findButton(container, '.incident-ack').click();
      await flush();
    });

    expect(gatewayMocks.ack).toHaveBeenCalledWith('demo-1');
    expect(gatewayMocks.list.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('ack 失败时把原因摊到面板上', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([incident()]));
    gatewayMocks.ack.mockRejectedValue(new Error('故障已恢复，时间线已定稿，无需标记'));

    await mount();
    await act(async () => {
      findButton(container, '.incident-ack').click();
      await flush();
    });

    expect(container.textContent).toContain('故障已恢复，时间线已定稿，无需标记');
  });

  it('已处理与已恢复的行不能再标记', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([
      incident({ id: 'acked', acknowledged: true }),
      incident({ id: 'closed', state: 'recovered', recoveredAt: '2026-08-19T10:30:00' }),
    ]));

    await mount();

    expect(findButton(container, '.incident-ack', 0).disabled).toBe(true);
    expect(findButton(container, '.incident-ack', 0).textContent).toContain('已处理');
    expect(findButton(container, '.incident-ack', 1).disabled).toBe(true);
  });

  it('点击触发诊断后立即置为诊断中并禁用', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([incident()]));
    gatewayMocks.diagnose.mockResolvedValue({ accepted: true });

    await mount();
    await act(async () => {
      findButton(container, '.incident-diagnose').click();
      await flush();
    });

    expect(gatewayMocks.diagnose).toHaveBeenCalledWith('demo-1');
    const button = findButton(container, '.incident-diagnose');
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain('诊断中');
  });

  it('服务端 409 归一为诊断中态，不报错', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([incident()]));
    gatewayMocks.diagnose.mockResolvedValue({ accepted: false });

    await mount();
    await act(async () => {
      findButton(container, '.incident-diagnose').click();
      await flush();
    });

    const button = findButton(container, '.incident-diagnose');
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain('诊断中');
    expect(container.querySelector('.incident-action-error')).toBeNull();
  });

  it('诊断请求失败时恢复按钮并显示原因', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([incident()]));
    gatewayMocks.diagnose.mockRejectedValue(new Error('本地 Server 当前不可用'));

    await mount();
    await act(async () => {
      findButton(container, '.incident-diagnose').click();
      await flush();
    });

    const button = findButton(container, '.incident-diagnose');
    expect(button.disabled).toBe(false);
    expect(button.textContent).toContain('触发诊断');
    expect(container.textContent).toContain('本地 Server 当前不可用');
  });

  it('服务端报告 running 时按钮同样是诊断中（无需本地点击）', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([
      incident({ diagnosis: { state: 'running', summary: '', finishedAt: null } }),
    ]));

    await mount();

    const button = findButton(container, '.incident-diagnose');
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain('诊断中');
  });

  it('alert_relay 来源两个操作都置灰并带悬浮提示', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([
      incident({ id: 'relay-1', source: 'alert_relay', title: '中继告警' }),
    ]));

    await mount();

    const diagnose = findButton(container, '.incident-diagnose');
    expect(diagnose.disabled).toBe(true);
    expect(diagnose.title).toContain('值班中继');
    expect(findButton(container, '.incident-ack').disabled).toBe(true);
  });

  it('迟到的旧响应不允许覆盖新状态', async () => {
    let resolveFirst!: (value: IncidentListResult) => void;
    const first = new Promise<IncidentListResult>((resolve) => { resolveFirst = resolve; });
    gatewayMocks.list
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce(listResult([incident({ title: '新状态' })]));

    await mount();
    // 首个请求悬空时手动刷新，新请求先返回
    await act(async () => {
      findButton(container, '.incident-refresh').click();
      await flush();
    });
    expect(container.textContent).toContain('新状态');

    // 旧请求这时才回来，必须被序号闸门丢弃
    await act(async () => {
      resolveFirst(listResult([incident({ title: '旧状态' })]));
      await flush();
    });

    expect(container.textContent).toContain('新状态');
    expect(container.textContent).not.toContain('旧状态');
  });

  it('每 2 秒轮询一次列表', async () => {
    vi.useFakeTimers();
    gatewayMocks.list.mockResolvedValue(listResult([]));

    await act(async () => {
      root.render(<IncidentPanel />);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(gatewayMocks.list).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(gatewayMocks.list).toHaveBeenCalledTimes(2);
  });

  it('Server 不可达时显示离线态', async () => {
    gatewayMocks.list.mockRejectedValue(new Error('连接本地 Server 超时'));

    await mount();

    expect(container.textContent).toContain('连接本地 Server 超时');
  });

  it('没有告警时显示空态', async () => {
    gatewayMocks.list.mockResolvedValue(listResult([]));

    await mount();

    expect(container.textContent).toContain('今天还没有告警记录');
  });
});

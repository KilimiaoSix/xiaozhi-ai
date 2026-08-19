/** @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PomodoroPanel } from './PomodoroPanel';
import type { PomodoroStatus } from './types';

const readyStatus = (overrides: Partial<PomodoroStatus> = {}): PomodoroStatus => ({
  deviceId: 'esp32-01',
  connected: true,
  active: true,
  phase: 'focus',
  paused: false,
  remainingS: 1230,
  totalS: 1500,
  round: 2,
  totalRounds: 4,
  ...overrides,
});

const gatewayMocks = vi.hoisted(() => ({
  listDevices: vi.fn(),
  getStatus: vi.fn(),
  sendCommand: vi.fn(),
}));

vi.mock('./services/pomodoroDesktopGateway', () => ({
  pomodoroDesktopGateway: gatewayMocks,
}));

const flush = async (): Promise<void> => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};

describe('PomodoroPanel', () => {
  let container: HTMLDivElement;
  let root: Root;
  let unmounted: boolean;

  beforeEach(() => {
    gatewayMocks.listDevices.mockReset();
    gatewayMocks.getStatus.mockReset();
    gatewayMocks.sendCommand.mockReset();
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    unmounted = false;
  });

  afterEach(async () => {
    if (!unmounted) await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('展示在线设备的相位、倒计时和轮次', async () => {
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [{ deviceId: 'esp32-01', connected: true, active: true }],
    });
    gatewayMocks.getStatus.mockResolvedValue(readyStatus());

    await act(async () => {
      root.render(<PomodoroPanel />);
      await flush();
    });

    expect(container.textContent).toContain('专注');
    expect(container.textContent).toContain('20:30');
    expect(container.textContent).toContain('第 2 / 4 轮');
  });

  it('两次轮询之间倒计时本地每秒推进', async () => {
    vi.useFakeTimers();
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [{ deviceId: 'esp32-01', connected: true, active: true }],
    });
    gatewayMocks.getStatus.mockResolvedValue(readyStatus());

    await act(async () => {
      root.render(<PomodoroPanel />);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(container.textContent).toContain('20:30');

    // 1s < 轮询间隔 2s：没有新快照,数字要靠本地推算走起来
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(container.textContent).toContain('20:29');
  });

  it('暂停时倒计时冻结,不做本地推进', async () => {
    vi.useFakeTimers();
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [{ deviceId: 'esp32-01', connected: true, active: true }],
    });
    gatewayMocks.getStatus.mockResolvedValue(readyStatus({ paused: true }));

    await act(async () => {
      root.render(<PomodoroPanel />);
      await vi.advanceTimersByTimeAsync(0);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(container.textContent).toContain('已暂停');
    expect(container.textContent).toContain('20:30');
  });

  it('没有已连接设备时显示离线态文案', async () => {
    gatewayMocks.listDevices.mockResolvedValue({ devices: [] });

    await act(async () => {
      root.render(<PomodoroPanel />);
      await flush();
    });

    expect(container.textContent).toContain('没有已连接的小智设备');
  });

  it('Server 不可达时显示离线态', async () => {
    gatewayMocks.listDevices.mockRejectedValue(new Error('本地 Server 当前不可用'));

    await act(async () => {
      root.render(<PomodoroPanel />);
      await flush();
    });

    expect(container.textContent).toContain('本地 Server 当前不可用');
  });

  it('点击暂停后下发命令并立即刷新状态', async () => {
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [{ deviceId: 'esp32-01', connected: true, active: true }],
    });
    gatewayMocks.getStatus.mockResolvedValue(readyStatus());
    gatewayMocks.sendCommand.mockResolvedValue(readyStatus({ paused: true }));

    await act(async () => {
      root.render(<PomodoroPanel />);
      await flush();
    });

    gatewayMocks.getStatus.mockResolvedValue(readyStatus({ paused: true }));

    const pauseButton = [...container.querySelectorAll('button')]
      .find((button) => button.textContent === '暂停');
    if (!(pauseButton instanceof HTMLButtonElement)) throw new Error('未找到暂停按钮');

    await act(async () => {
      pauseButton.click();
      await flush();
    });

    expect(gatewayMocks.sendCommand).toHaveBeenCalledWith('esp32-01', 'pause');
    expect(container.textContent).toContain('已暂停');
    expect(container.textContent).toContain('继续');
  });

  it('设备离线但会话仍活跃时保留倒计时与暂停/停止入口', async () => {
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [{ deviceId: 'esp32-01', connected: false, active: true }],
    });
    gatewayMocks.getStatus.mockResolvedValue(readyStatus({ connected: false }));

    await act(async () => {
      root.render(<PomodoroPanel />);
      await flush();
    });

    expect(gatewayMocks.getStatus).toHaveBeenCalledWith('esp32-01');
    expect(container.textContent).toContain('20:30');
    expect(container.textContent).toContain('设备离线，会话仍在运行');
    expect(container.textContent).not.toContain('没有已连接的小智设备');

    const buttons = [...container.querySelectorAll('button')];
    const pauseButton = buttons.find((button) => button.textContent === '暂停');
    const stopButton = buttons.find((button) => button.textContent === '停止');
    expect(pauseButton?.disabled).toBe(false);
    expect(stopButton?.disabled).toBe(false);

    await act(async () => {
      pauseButton?.click();
      await flush();
    });
    expect(gatewayMocks.sendCommand).toHaveBeenCalledWith('esp32-01', 'pause');
  });

  it('在线设备优先于仅有活跃会话的离线设备', async () => {
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [
        { deviceId: 'esp32-offline', connected: false, active: true },
        { deviceId: 'esp32-online', connected: true, active: true },
      ],
    });
    gatewayMocks.getStatus.mockResolvedValue(readyStatus({ deviceId: 'esp32-online' }));

    await act(async () => {
      root.render(<PomodoroPanel />);
      await flush();
    });

    expect(gatewayMocks.getStatus).toHaveBeenCalledWith('esp32-online');
  });

  it('命令失败时把错误摊到面板上而不是变成未处理的 rejection', async () => {
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [{ deviceId: 'esp32-01', connected: true, active: true }],
    });
    gatewayMocks.getStatus.mockResolvedValue(readyStatus());
    gatewayMocks.sendCommand.mockRejectedValue(new Error('Server 请求失败（500）'));

    await act(async () => {
      root.render(<PomodoroPanel />);
      await flush();
    });

    const stopButton = [...container.querySelectorAll('button')]
      .find((button) => button.textContent === '停止');
    if (!(stopButton instanceof HTMLButtonElement)) throw new Error('未找到停止按钮');

    await act(async () => {
      stopButton.click();
      await flush();
    });

    expect(container.textContent).toContain('Server 请求失败（500）');
  });

  it('设备在线且空闲时提供开始专注入口', async () => {
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [{ deviceId: 'esp32-01', connected: true, active: false }],
    });
    gatewayMocks.getStatus.mockResolvedValue(
      readyStatus({ active: false, phase: 'idle', paused: false, remainingS: 0 }),
    );
    gatewayMocks.sendCommand.mockResolvedValue(readyStatus());

    await act(async () => {
      root.render(<PomodoroPanel />);
      await flush();
    });

    const startButton = [...container.querySelectorAll('button')]
      .find((button) => button.textContent === '开始专注');
    if (!(startButton instanceof HTMLButtonElement)) throw new Error('未找到开始专注按钮');
    expect(startButton.disabled).toBe(false);

    await act(async () => {
      startButton.click();
      await flush();
    });

    expect(gatewayMocks.sendCommand).toHaveBeenCalledWith('esp32-01', 'start');
  });

  it('迟到的旧轮询响应不覆盖新状态', async () => {
    vi.useFakeTimers();
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [{ deviceId: 'esp32-01', connected: true, active: true }],
    });
    let resolveStale: ((status: PomodoroStatus) => void) | undefined;
    gatewayMocks.getStatus
      .mockImplementationOnce(() => new Promise<PomodoroStatus>((resolve) => {
        resolveStale = resolve;
      }))
      .mockResolvedValue(readyStatus({ paused: true }));

    await act(async () => {
      root.render(<PomodoroPanel />);
      await vi.advanceTimersByTimeAsync(0);
    });

    // 第一轮还卡在 getStatus 上，第二轮已经拿到"已暂停"的新快照
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(container.textContent).toContain('已暂停');

    await act(async () => {
      resolveStale?.(readyStatus({ paused: false }));
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(container.textContent).toContain('已暂停');
  });

  it('卸载后停止轮询', async () => {
    vi.useFakeTimers();
    gatewayMocks.listDevices.mockResolvedValue({
      devices: [{ deviceId: 'esp32-01', connected: true, active: true }],
    });
    gatewayMocks.getStatus.mockResolvedValue(readyStatus());

    await act(async () => {
      root.render(<PomodoroPanel />);
      await vi.advanceTimersByTimeAsync(0);
    });

    const callsBeforeUnmount = gatewayMocks.listDevices.mock.calls.length;

    await act(async () => {
      root.unmount();
    });
    unmounted = true;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(gatewayMocks.listDevices.mock.calls.length).toBe(callsBeforeUnmount);
  });
});

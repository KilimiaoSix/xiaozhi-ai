/** @vitest-environment jsdom */

// 隐藏视频 play() 失败处理的独立用例。
//
// 真机日志刷过未处理的 AbortError：StrictMode 与换流会先卸载再重挂，
// 挂起的 play() 被 srcObject 置空打断。中断属正常时序要静默；
// 真正的播放失败必须留下 console.warn 线索，不能一起吞掉。
// 单独成文件是为了不与主测试文件的并行改动纠缠，setup 只复刻必需的桩。

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CameraMonitoringProvider,
  useCameraMonitoring,
  type CameraMonitoringValue,
} from './CameraMonitoringProvider';

vi.mock('../services/videoFrameProducer', () => ({
  VideoFrameProducer: class {
    start = vi.fn();
    stop = vi.fn();
  },
}));

const makeStream = () => {
  const track = new (class extends EventTarget {
    stop = vi.fn();
    getSettings() { return { deviceId: 'camera-1' }; }
  })();
  return {
    getTracks: () => [track],
    getVideoTracks: () => [track],
  } as unknown as MediaStream;
};

describe('CameraMonitoringProvider 隐藏视频 play() 失败处理', () => {
  let container: HTMLDivElement;
  let root: Root;
  let current: CameraMonitoringValue | null;

  const recognition = {
    start: vi.fn(async () => undefined),
    sendFrame: vi.fn(async () => 'sent' as const),
    stop: vi.fn(async () => undefined),
    onEvent: vi.fn(() => vi.fn()),
  };
  // OS 层权限闸门在本文件的用例里恒为放行,专注验证 play() 失败处理本身
  const camera = {
    getPermissionStatus: vi.fn(async () => 'granted' as const),
    requestPermission: vi.fn(async () => 'granted' as const),
  };

  function Probe() {
    current = useCameraMonitoring();
    return null;
  }

  beforeEach(() => {
    current = null;
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: vi.fn(async () => makeStream()),
        enumerateDevices: vi.fn(async () => []),
      },
    });
    Object.defineProperty(window, 'xiaofei', {
      configurable: true,
      value: { camera: { ...camera, recognition } },
    });
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const render = async () => {
    await act(async () => root.render(
      <CameraMonitoringProvider>
        <Probe />
      </CameraMonitoringProvider>,
    ));
  };

  const flush = async () => {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  };

  it('非 AbortError 的播放失败要落 console.warn，且不打断监测启动', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const play = vi.fn().mockRejectedValue(new Error('decoder wedged'));
    Object.defineProperty(HTMLMediaElement.prototype, 'play', {
      configurable: true,
      value: play,
    });

    await render();
    await act(async () => current!.startMonitoring());
    await flush();

    expect(play).toHaveBeenCalled();
    expect(current!.enabled).toBe(true);
    expect(warn).toHaveBeenCalledWith(
      '隐藏视频启动播放失败',
      expect.objectContaining({ message: 'decoder wedged' }),
    );
  });

  it('AbortError 静默忽略，不产生告警噪音', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const play = vi.fn().mockRejectedValue(
      new DOMException('interrupted by a new load request', 'AbortError'),
    );
    Object.defineProperty(HTMLMediaElement.prototype, 'play', {
      configurable: true,
      value: play,
    });

    await render();
    await act(async () => current!.startMonitoring());
    await flush();

    expect(play).toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  it('play() 返回 undefined（jsdom 的默认行为）时不崩', async () => {
    Object.defineProperty(HTMLMediaElement.prototype, 'play', {
      configurable: true,
      value: vi.fn(() => undefined),
    });

    await render();
    await act(async () => current!.startMonitoring());
    await flush();

    expect(current!.enabled).toBe(true);
  });
});

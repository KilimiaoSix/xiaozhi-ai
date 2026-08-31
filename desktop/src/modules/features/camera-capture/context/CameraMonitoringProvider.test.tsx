/** @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CameraMonitoringProvider,
  useCameraMonitoring,
  type CameraMonitoringValue,
} from './CameraMonitoringProvider';
import type { CameraPermissionStatus } from '../types';

const producerMocks = vi.hoisted(() => ({
  start: vi.fn(),
  stop: vi.fn(),
}));

vi.mock('../services/videoFrameProducer', () => ({
  VideoFrameProducer: class {
    start = producerMocks.start;
    stop = producerMocks.stop;
  },
}));

class FakeTrack extends EventTarget {
  stop = vi.fn();
  getSettings() { return { deviceId: 'camera-1' }; }
}

const makeStream = () => {
  const track = new FakeTrack();
  return {
    track,
    stream: {
      getTracks: () => [track],
      getVideoTracks: () => [track],
    } as unknown as MediaStream,
  };
};

describe('CameraMonitoringProvider', () => {
  let container: HTMLDivElement;
  let root: Root;
  let current: CameraMonitoringValue | null;
  let recognitionListener: ((event: Record<string, unknown>) => void) | null;
  let streams: ReturnType<typeof makeStream>[];
  const recognition = {
    start: vi.fn(async () => undefined),
    sendFrame: vi.fn(async () => 'sent' as const),
    stop: vi.fn(async () => undefined),
    onEvent: vi.fn((listener: (event: Record<string, unknown>) => void) => {
      recognitionListener = listener;
      return vi.fn();
    }),
  };
  // OS 层权限闸门:默认放行(granted),权限拒绝场景在各用例里用
  // mockResolvedValueOnce 覆盖一次,不影响其余用例的默认路径。
  const camera = {
    getPermissionStatus: vi.fn(async (): Promise<CameraPermissionStatus> => 'granted'),
    requestPermission: vi.fn(async (): Promise<CameraPermissionStatus> => 'granted'),
  };

  function Probe() {
    current = useCameraMonitoring();
    return <span>{current.enabled ? 'on' : 'off'}</span>;
  }

  const render = async (showProbe = true) => {
    await act(async () => root.render(
      <CameraMonitoringProvider>
        {showProbe && <Probe />}
      </CameraMonitoringProvider>,
    ));
  };

  beforeEach(() => {
    current = null;
    recognitionListener = null;
    streams = [makeStream(), makeStream(), makeStream()];
    recognition.start.mockClear();
    recognition.sendFrame.mockClear();
    recognition.stop.mockClear();
    recognition.onEvent.mockClear();
    camera.getPermissionStatus.mockClear();
    camera.requestPermission.mockClear();
    producerMocks.start.mockClear();
    producerMocks.stop.mockClear();
    let streamIndex = 0;
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: vi.fn(async () => streams[streamIndex++].stream),
        enumerateDevices: vi.fn(async () => []),
      },
    });
    Object.defineProperty(window, 'xiaofei', {
      configurable: true,
      value: { camera: { ...camera, recognition } },
    });
    Object.defineProperty(HTMLMediaElement.prototype, 'play', {
      configurable: true,
      value: vi.fn(async () => undefined),
    });
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    // 本仓库的 jsdom 没有可用的 localStorage 实现;装一个每用例独立的内存版,
    // 否则监测意图会在用例间残留,后续用例挂载即自动开启监测
    const storage = new Map<string, string>();
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => void storage.set(key, String(value)),
        removeItem: (key: string) => void storage.delete(key),
        clear: () => storage.clear(),
      },
    });
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('keeps monitoring active when the camera page consumer unmounts', async () => {
    await render();
    await act(async () => current!.startMonitoring());
    await render(false);

    expect(recognition.start).toHaveBeenCalledWith({
      mode: 'monitoring',
      workstationId: 'desktop-local',
    });
    expect(recognition.stop).not.toHaveBeenCalled();
    expect(streams[0].track.stop).not.toHaveBeenCalled();
  });

  it('keeps the user intent enabled across server errors and reconnects', async () => {
    await render();
    await act(async () => current!.startMonitoring());

    await act(async () => recognitionListener?.({
      type: 'error', code: 'INFERENCE_ERROR', message: 'temporary', retryable: true,
    }));
    expect(current!.enabled).toBe(true);
    expect(current!.errorMessage).toBe('temporary');

    await act(async () => recognitionListener?.({
      type: 'connection', state: 'reconnecting', retryInMs: 2000,
    }));
    expect(current!.connection).toBe('reconnecting');
    expect(recognition.stop).not.toHaveBeenCalled();
  });

  it('derives face presence from face_count and preserves the server matched flag', async () => {
    await render();
    await act(async () => current!.startMonitoring());

    await act(async () => recognitionListener?.({
      type: 'recognition_result',
      processed_at: '2026-08-18T12:00:00Z',
      presence: { state: 'present', changed: true },
      identity: {
        state: 'unknown', face_count: 1, face_detected: false,
        similarity: 0.99, matched: false,
      },
      metrics: { processed_frames: 4, server_dropped: 1 },
    }));

    expect(current!.identity).toMatchObject({
      state: 'unknown', faceCount: 1, faceDetected: true,
      similarity: 0.99, matched: false,
    });
    expect(current!.metrics).toMatchObject({
      processedFrames: 4, serverDropped: 1,
    });
  });

  it('restarts the selected camera when its video track ends', async () => {
    await render();
    await act(async () => current!.startMonitoring());

    await act(async () => streams[0].track.dispatchEvent(new Event('ended')));

    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2);
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenLastCalledWith(
      expect.objectContaining({
        video: expect.objectContaining({ deviceId: { exact: 'camera-1' } }),
      }),
    );
    expect(current!.enabled).toBe(true);
    expect(recognition.stop).not.toHaveBeenCalled();
  });

  it('keeps monitoring enabled and retries a transient camera startup failure', async () => {
    vi.useFakeTimers();
    vi.mocked(navigator.mediaDevices.getUserMedia)
      .mockRejectedValueOnce(new DOMException('busy', 'NotReadableError'));
    await render();
    await act(async () => current!.startMonitoring());

    expect(current!.enabled).toBe(true);
    expect(current!.errorMessage).toContain('占用');
    expect(recognition.stop).not.toHaveBeenCalled();

    await act(async () => vi.advanceTimersByTimeAsync(2000));

    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2);
    expect(recognition.start).toHaveBeenCalledWith({
      mode: 'monitoring', workstationId: 'desktop-local',
    });
    expect(current!.enabled).toBe(true);
  });

  it('only manual stop closes monitoring during the provider lifetime', async () => {
    await render();
    await act(async () => current!.startMonitoring());
    await act(async () => current!.stopMonitoring());

    expect(recognition.stop).toHaveBeenCalledOnce();
    expect(streams[0].track.stop).toHaveBeenCalledOnce();
    expect(current!.enabled).toBe(false);
  });

  it('releases the local camera even when transport stop fails', async () => {
    recognition.stop.mockRejectedValueOnce(new Error('ipc unavailable'));
    await render();
    await act(async () => current!.startMonitoring());
    await act(async () => current!.stopMonitoring());

    expect(streams[0].track.stop).toHaveBeenCalledOnce();
    expect(current!.enabled).toBe(false);
    expect(current!.connection).toBe('idle');
  });

  it('cleans up the transport and camera when the application root unmounts', async () => {
    await render();
    await act(async () => current!.startMonitoring());

    await act(async () => root.unmount());
    root = createRoot(container);

    expect(recognition.stop).toHaveBeenCalledOnce();
    expect(streams[0].track.stop).toHaveBeenCalledOnce();
  });

  it('tracks enrollment progress and stops after completion', async () => {
    await render();
    await act(async () => current!.startEnrollment('主人'));
    await act(async () => recognitionListener?.({
      type: 'enrollment_progress', accepted: 7, required: 20, reason: 'accepted',
    }));
    expect(current!.enrollment).toMatchObject({ status: 'running', accepted: 7 });

    await act(async () => recognitionListener?.({
      type: 'enrollment_complete', profile_id: 'owner', sample_id: 'sample-1',
      stored_at: '2026-08-18T12:00:00Z', sample_count: 18,
    }));

    expect(current!.enrollment).toMatchObject({
      status: 'success', sampleId: 'sample-1', sampleCount: 18,
    });
    expect(recognition.stop).toHaveBeenCalledOnce();
    expect(streams[0].track.stop).toHaveBeenCalledOnce();
  });

  it('releases an enrollment session after a server error', async () => {
    await render();
    await act(async () => current!.startEnrollment('主人'));
    await act(async () => recognitionListener?.({
      type: 'error', code: 'MODEL_UNAVAILABLE', message: 'model unavailable',
      retryable: true,
    }));

    expect(current!.enrollment.status).toBe('error');
    expect(recognition.stop).toHaveBeenCalledOnce();
    expect(streams[0].track.stop).toHaveBeenCalledOnce();
  });

  describe('监测意图持久化', () => {
    it('startMonitoring 落盘意图,stopMonitoring 清除', async () => {
      await render();
      await act(async () => current!.startMonitoring());
      expect(window.localStorage.getItem('xiaofei.camera.monitoring-intent')).toBe('on');

      await act(async () => current!.stopMonitoring());
      expect(window.localStorage.getItem('xiaofei.camera.monitoring-intent')).toBe('off');
    });

    it('上次意图为 on 时挂载自动恢复监测', async () => {
      window.localStorage.setItem('xiaofei.camera.monitoring-intent', 'on');
      await render();

      expect(recognition.start).toHaveBeenCalledWith({
        mode: 'monitoring',
        workstationId: 'desktop-local',
      });
      expect(current!.enabled).toBe(true);
    });

    it('无意图记录时挂载保持关闭(首次使用不擅自开摄像头)', async () => {
      await render();

      expect(recognition.start).not.toHaveBeenCalled();
      expect(current!.enabled).toBe(false);
    });

    it('意图为 off 时挂载不恢复', async () => {
      window.localStorage.setItem('xiaofei.camera.monitoring-intent', 'off');
      await render();

      expect(recognition.start).not.toHaveBeenCalled();
      expect(current!.enabled).toBe(false);
    });
  });

  describe('OS 层摄像头权限闸门', () => {
    it('startMonitoring: 权限 denied 时不调 getUserMedia,直接进入权限错误态', async () => {
      camera.getPermissionStatus.mockResolvedValueOnce('denied');
      await render();
      await act(async () => current!.startMonitoring());

      expect(camera.getPermissionStatus).toHaveBeenCalledOnce();
      expect(camera.requestPermission).not.toHaveBeenCalled();
      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
      expect(current!.errorMessage).toBe('摄像头权限未开启');
      // 意图仍是「持续意图」语义:权限失败不应清掉用户想监测的意图
      expect(current!.enabled).toBe(true);
    });

    it('startMonitoring: 权限 restricted 时不调 getUserMedia,直接进入权限错误态', async () => {
      camera.getPermissionStatus.mockResolvedValueOnce('restricted');
      await render();
      await act(async () => current!.startMonitoring());

      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
      expect(current!.errorMessage).toBe('摄像头权限未开启');
    });

    it('startMonitoring: not-determined 时先 requestPermission,拒绝后同样进入权限错误态', async () => {
      camera.getPermissionStatus.mockResolvedValueOnce('not-determined');
      camera.requestPermission.mockResolvedValueOnce('denied');
      await render();
      await act(async () => current!.startMonitoring());

      expect(camera.requestPermission).toHaveBeenCalledOnce();
      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
      expect(current!.errorMessage).toBe('摄像头权限未开启');
    });

    it('startMonitoring: 权限 granted 时正常放行,走原有 getUserMedia 路径', async () => {
      camera.getPermissionStatus.mockResolvedValueOnce('granted');
      await render();
      await act(async () => current!.startMonitoring());

      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce();
      expect(current!.errorMessage).toBe('');
      expect(current!.enabled).toBe(true);
    });

    it('startEnrollment: 权限 denied 时不调 getUserMedia 也不建立识别会话,直接进入权限错误态', async () => {
      camera.getPermissionStatus.mockResolvedValueOnce('denied');
      await render();
      await act(async () => current!.startEnrollment('主人'));

      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
      expect(recognition.start).not.toHaveBeenCalled();
      expect(current!.enrollment.status).toBe('error');
      expect(current!.errorMessage).toBe('摄像头权限未开启');
    });

    it('startEnrollment: 权限 granted 时正常放行,走原有路径', async () => {
      camera.getPermissionStatus.mockResolvedValueOnce('granted');
      await render();
      await act(async () => current!.startEnrollment('主人'));

      expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce();
      expect(recognition.start).toHaveBeenCalledWith({
        mode: 'enrollment',
        workstationId: 'desktop-local',
        displayName: '主人',
      });
      expect(current!.errorMessage).toBe('');
    });

    it('闸门下沉到 startCamera:2 秒恢复定时器在权限持续 denied 时不会绕过闸门白调 getUserMedia', async () => {
      vi.useFakeTimers();
      // 权限恒 denied：初次 startMonitoring 一次 + 两轮 2 秒恢复定时器各一次
      camera.getPermissionStatus
        .mockResolvedValueOnce('denied')
        .mockResolvedValueOnce('denied')
        .mockResolvedValueOnce('denied');
      await render();
      await act(async () => current!.startMonitoring());

      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
      expect(current!.errorMessage).toBe('摄像头权限未开启');

      await act(async () => vi.advanceTimersByTimeAsync(2000));
      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
      expect(current!.errorMessage).toBe('摄像头权限未开启');

      await act(async () => vi.advanceTimersByTimeAsync(2000));
      expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
      expect(current!.errorMessage).toBe('摄像头权限未开启');
      expect(camera.getPermissionStatus).toHaveBeenCalledTimes(3);
    });
  });

});

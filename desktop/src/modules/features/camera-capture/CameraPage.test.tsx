/** @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CameraPage } from './CameraPage';

const monitoring = vi.hoisted(() => ({
  enabled: false,
  stream: null as MediaStream | null,
  devices: [] as MediaDeviceInfo[],
  selectedDeviceId: '',
  connection: 'idle' as const,
  presence: { state: 'starting' as const, changed: false },
  identity: {
    state: 'starting' as const, faceCount: 0, faceDetected: false, matched: false,
  },
  metrics: {
    sentFrames: 0, clientDropped: 0, processedFrames: 0,
    serverDropped: 0, lastResultAt: '',
  },
  enrollment: {
    status: 'idle' as const, accepted: 0, required: 20, reason: '',
    sampleId: '', sampleCount: 0, storedAt: '',
  },
  errorMessage: '',
  startMonitoring: vi.fn(async () => undefined),
  stopMonitoring: vi.fn(async () => undefined),
  startEnrollment: vi.fn(async () => undefined),
  cancelEnrollment: vi.fn(async () => undefined),
  selectDevice: vi.fn(async () => undefined),
}));

const wellbeing = vi.hoisted(() => ({
  sendTest: vi.fn(async () => ({ deviceId: 'robot-1' })),
}));

vi.mock('./context/CameraMonitoringProvider', () => ({
  useCameraMonitoring: () => monitoring,
}));

vi.mock('./services/wellbeingDesktopGateway', () => ({
  wellbeingDesktopGateway: wellbeing,
}));

vi.mock('./components/CameraPreview', () => ({
  CameraPreview: (props: { activeLabel: string }) => (
    <div data-testid="camera-preview">{props.activeLabel}</div>
  ),
}));

const findButton = (container: HTMLElement, label: string): HTMLButtonElement => {
  const button = [...container.querySelectorAll('button')]
    .find((candidate) => candidate.textContent?.trim() === label);
  if (!(button instanceof HTMLButtonElement)) throw new Error(`Button not found: ${label}`);
  return button;
};

describe('CameraPage persistent monitoring controls', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    monitoring.enabled = false;
    monitoring.startMonitoring.mockClear();
    monitoring.stopMonitoring.mockClear();
    monitoring.startEnrollment.mockClear();
    monitoring.cancelEnrollment.mockClear();
    wellbeing.sendTest.mockClear();
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('starts enrollment as a multi-frame stream', async () => {
    await act(async () => root.render(<CameraPage />));
    await act(async () => findButton(container, '开始注册').click());

    expect(monitoring.startEnrollment).toHaveBeenCalledWith('主人');
  });

  it('does not stop monitoring when the page unmounts', async () => {
    monitoring.enabled = true;
    await act(async () => root.render(<CameraPage />));

    const enrollmentTab = findButton(container, '主人录入');
    expect(enrollmentTab.disabled).toBe(true);
    expect(container.textContent).toContain('监测中');
    await act(async () => root.unmount());
    root = createRoot(container);

    expect(monitoring.stopMonitoring).not.toHaveBeenCalled();
  });

  it('sends the selected care test from the monitoring panel', async () => {
    await act(async () => root.render(<CameraPage />));
    await act(async () => findButton(container, '实时监测').click());
    await act(async () => findButton(container, '测试久坐提醒').click());

    expect(wellbeing.sendTest).toHaveBeenCalledWith('long_work');
    expect(container.textContent).toContain('测试提醒已发送');
  });

  it('shows delivery failures next to the test button', async () => {
    wellbeing.sendTest.mockRejectedValueOnce(new Error('没有在线机器人'));
    await act(async () => root.render(<CameraPage />));
    await act(async () => findButton(container, '实时监测').click());
    await act(async () => findButton(container, '测试久坐提醒').click());

    expect(container.textContent).toContain('没有在线机器人');
  });
});

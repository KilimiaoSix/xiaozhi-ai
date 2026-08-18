/** @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CameraPage } from './CameraPage';

const cameraMocks = vi.hoisted(() => ({
  captureJpeg: vi.fn(async () => new ArrayBuffer(8)),
  start: vi.fn(async () => undefined),
  stop: vi.fn(),
}));

vi.mock('./components/CameraPreview', async () => {
  const React = await import('react');
  return {
    CameraPreview: React.forwardRef(function MockCameraPreview(
      props: { activeLabel: string },
      ref,
    ) {
      React.useImperativeHandle(ref, () => ({
        captureJpeg: cameraMocks.captureJpeg,
      }));
      return <div data-testid="camera-preview">{props.activeLabel}</div>;
    }),
  };
});

vi.mock('./hooks/useCameraStream', () => ({
  useCameraStream: () => ({
    stream: {} as MediaStream,
    devices: [],
    selectedDeviceId: '',
    errorMessage: '',
    start: cameraMocks.start,
    stop: cameraMocks.stop,
  }),
}));

vi.mock('./services/cameraDesktopGateway', () => ({
  cameraDesktopGateway: {
    getPermissionStatus: vi.fn(async () => 'granted'),
    requestPermission: vi.fn(async () => 'granted'),
    enrollOwner: vi.fn(),
    uploadMonitoringFrame: vi.fn(),
  },
}));

const findButton = (container: HTMLElement, label: string): HTMLButtonElement => {
  const button = [...container.querySelectorAll('button')]
    .find((candidate) => candidate.textContent?.trim() === label);
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`Button not found: ${label}`);
  }
  return button;
};

describe('CameraPage owner enrollment camera lifecycle', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(async () => {
    cameraMocks.captureJpeg.mockClear();
    cameraMocks.start.mockClear();
    cameraMocks.stop.mockClear();
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:owner-photo');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<CameraPage />));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('stops the camera after taking an owner photo', async () => {
    await act(async () => {
      findButton(container, '拍照').click();
    });

    expect(cameraMocks.captureJpeg).toHaveBeenCalledOnce();
    expect(cameraMocks.stop).toHaveBeenCalledOnce();
    expect(container.textContent).toContain('照片已拍摄 · 摄像头已关闭');
  });

  it('starts the camera again when retaking the owner photo', async () => {
    await act(async () => {
      findButton(container, '拍照').click();
    });
    cameraMocks.start.mockClear();

    await act(async () => {
      findButton(container, '重拍').click();
    });

    expect(cameraMocks.start).toHaveBeenCalledOnce();
  });
});

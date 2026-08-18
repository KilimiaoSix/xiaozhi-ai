import { describe, expect, it } from 'vitest';

import { cameraReducer, initialCameraState } from './cameraReducer';

describe('cameraReducer', () => {
  it('从实时监测切到主人录入时停止监测', () => {
    const monitoring = cameraReducer(initialCameraState, {
      type: 'monitoring-started',
    });
    const enrolled = cameraReducer(monitoring, {
      type: 'mode-selected',
      mode: 'enrollment',
    });

    expect(enrolled.mode).toBe('enrollment');
    expect(enrolled.monitoring.status).toBe('idle');
  });

  it('记录主人照片上传成功结果', () => {
    const uploading = cameraReducer(initialCameraState, {
      type: 'enrollment-uploading',
    });
    const success = cameraReducer(uploading, {
      type: 'enrollment-succeeded',
      sampleId: 'sample-1',
    });

    expect(success.enrollment).toMatchObject({
      status: 'success',
      sampleId: 'sample-1',
    });
    expect(success.errorMessage).toBe('');
  });

  it('记录监测指标和最近成功时间', () => {
    const running = cameraReducer(initialCameraState, {
      type: 'monitoring-started',
    });
    const updated = cameraReducer(running, {
      type: 'monitoring-metrics',
      sentFrames: 4,
      droppedFrames: 2,
      lastSuccessAt: '12:00:04',
    });

    expect(updated.monitoring).toMatchObject({
      status: 'running',
      sentFrames: 4,
      droppedFrames: 2,
      lastSuccessAt: '12:00:04',
    });
  });
});

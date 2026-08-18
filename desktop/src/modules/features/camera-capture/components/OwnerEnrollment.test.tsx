import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { OwnerEnrollment } from './OwnerEnrollment';

describe('OwnerEnrollment', () => {
  it('shows accepted samples and the current quality guidance', () => {
    const markup = renderToStaticMarkup(
      <OwnerEnrollment
        displayName="主人"
        enrollment={{
          status: 'running', accepted: 7, required: 20, reason: 'multiple_faces',
          sampleId: '', sampleCount: 0, storedAt: '',
        }}
        disabled={false}
        onDisplayNameChange={vi.fn()}
        onStart={vi.fn()}
        onCancel={vi.fn()}
        onReset={vi.fn()}
      />,
    );

    expect(markup).toContain('7 / 20');
    expect(markup).toContain('画面中请只保留一张人脸');
    expect(markup).toContain('取消注册');
  });

  it('disables enrollment while monitoring is enabled', () => {
    const markup = renderToStaticMarkup(
      <OwnerEnrollment
        displayName="主人"
        enrollment={{
          status: 'idle', accepted: 0, required: 20, reason: '',
          sampleId: '', sampleCount: 0, storedAt: '',
        }}
        disabled
        onDisplayNameChange={vi.fn()}
        onStart={vi.fn()}
        onCancel={vi.fn()}
        onReset={vi.fn()}
      />,
    );

    expect(markup).toContain('请先关闭实时监测');
    expect(markup).toContain('disabled=""');
  });

  it('shows the completed multi-frame sample result', () => {
    const markup = renderToStaticMarkup(
      <OwnerEnrollment
        displayName="主人"
        enrollment={{
          status: 'success', accepted: 20, required: 20, reason: 'complete',
          sampleId: 'sample-1', sampleCount: 18, storedAt: '2026-08-18T12:00:00Z',
        }}
        disabled={false}
        onDisplayNameChange={vi.fn()}
        onStart={vi.fn()}
        onCancel={vi.fn()}
        onReset={vi.fn()}
      />,
    );

    expect(markup).toContain('已保存');
    expect(markup).toContain('18 个有效样本');
    expect(markup).toContain('sample-1');
  });
});

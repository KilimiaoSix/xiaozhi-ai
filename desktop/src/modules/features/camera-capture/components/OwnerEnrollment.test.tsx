import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { OwnerEnrollment } from './OwnerEnrollment';

describe('OwnerEnrollment', () => {
  it('keeps photo actions visible after the camera is released', () => {
    const markup = renderToStaticMarkup(
      <OwnerEnrollment
        displayName="主人"
        status="captured"
        sampleId=""
        cameraReady={false}
        onDisplayNameChange={vi.fn()}
        onEnableCamera={vi.fn()}
        onCapture={vi.fn()}
        onRetake={vi.fn()}
        onUpload={vi.fn()}
      />,
    );

    expect(markup).toContain('重拍');
    expect(markup).toContain('确认并上传');
    expect(markup).not.toContain('启用摄像头');
  });
});

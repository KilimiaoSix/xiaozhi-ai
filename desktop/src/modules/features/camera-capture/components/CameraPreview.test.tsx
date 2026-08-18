import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { CameraPreview } from './CameraPreview';

describe('CameraPreview', () => {
  it('keeps the video element mounted while showing a captured photo', () => {
    const markup = renderToStaticMarkup(
      <CameraPreview
        stream={null}
        capturedUrl="blob:owner-photo"
        activeLabel="摄像头使用中"
        monitoring={false}
      />,
    );

    expect(markup).toContain('<video');
    expect(markup).toContain('<img');
  });
});

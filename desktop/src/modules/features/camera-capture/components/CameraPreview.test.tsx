import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { CameraPreview } from './CameraPreview';

describe('CameraPreview', () => {
  it('shows the continuous 5 FPS stream state', () => {
    const markup = renderToStaticMarkup(
      <CameraPreview
        stream={{} as MediaStream}
        activeLabel="监测中"
        monitoring
        enrollment={false}
      />,
    );

    expect(markup).toContain('<video');
    expect(markup).toContain('5 FPS · JPEG');
    expect(markup).not.toContain('<img');
  });
});

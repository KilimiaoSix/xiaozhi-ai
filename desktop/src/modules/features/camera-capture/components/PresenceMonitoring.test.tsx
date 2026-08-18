import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { PresenceMonitoring } from './PresenceMonitoring';

const metrics = {
  sentFrames: 12,
  clientDropped: 2,
  processedFrames: 9,
  serverDropped: 1,
  lastResultAt: '2026-08-18T12:00:00Z',
};

describe('PresenceMonitoring', () => {
  it('renders body, face, owner match, server similarity, and stream metrics', () => {
    const markup = renderToStaticMarkup(
      <PresenceMonitoring
        enabled
        connection="online"
        presence={{ state: 'present', changed: true }}
        identity={{
          state: 'owner', faceCount: 1, faceDetected: true, matched: true,
          similarity: 0.731245, threshold: 0.45,
        }}
        metrics={metrics}
        onToggle={vi.fn()}
      />,
    );

    expect(markup).toContain('有人');
    expect(markup).toContain('检测到人脸');
    expect(markup).toContain('主人');
    expect(markup).toContain('73.1%');
    expect(markup).toContain('已匹配');
    expect(markup).toContain('12');
    expect(markup).toContain('9');
  });

  it('does not infer a match from similarity and hides missing similarity', () => {
    const markup = renderToStaticMarkup(
      <PresenceMonitoring
        enabled
        connection="reconnecting"
        presence={{ state: 'absent', changed: false }}
        identity={{
          state: 'multiple_faces', faceCount: 2, faceDetected: true, matched: false,
        }}
        metrics={{ ...metrics, lastResultAt: '' }}
        onToggle={vi.fn()}
      />,
    );

    expect(markup).toContain('重连中');
    expect(markup).toContain('无人');
    expect(markup).toContain('多张人脸');
    expect(markup).toContain('未匹配');
    expect(markup).not.toContain('%');
  });
});

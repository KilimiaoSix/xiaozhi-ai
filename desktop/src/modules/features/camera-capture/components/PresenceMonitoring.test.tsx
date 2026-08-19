/** @vitest-environment jsdom */

import { act } from 'react';
import { createRoot } from 'react-dom/client';
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
        onTest={vi.fn()}
        testingKind={null}
        testMessage=""
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
        onTest={vi.fn()}
        testingKind={null}
        testMessage=""
      />,
    );

    expect(markup).toContain('重连中');
    expect(markup).toContain('无人');
    expect(markup).toContain('多张人脸');
    expect(markup).toContain('未匹配');
    expect(markup).not.toContain('%');
  });

  it('offers all care tests and sends the selected kind', async () => {
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    const onTest = vi.fn(async () => undefined);

    await act(async () => root.render(
      <PresenceMonitoring
        enabled={false}
        connection="idle"
        presence={{ state: 'starting', changed: false }}
        identity={{
          state: 'starting', faceCount: 0, faceDetected: false, matched: false,
        }}
        metrics={metrics}
        onToggle={vi.fn()}
        onTest={onTest}
        testingKind={null}
        testMessage=""
      />,
    ));

    const labels = [...container.querySelectorAll('.camera-test-action button')]
      .map((candidate) => candidate.textContent?.trim());
    expect(labels).toEqual([
      '测试久坐提醒',
      '测试通勤安全',
      '测试 21 点提醒',
      '测试深夜强提醒',
      '测试暖心加油',
    ]);
    const button = [...container.querySelectorAll('button')]
      .find((candidate) => candidate.textContent?.includes('测试通勤安全'));
    if (!(button instanceof HTMLButtonElement)) {
      throw new Error('Button not found: 测试通勤安全');
    }
    await act(async () => button.click());

    expect(onTest).toHaveBeenCalledWith('commute_safety');
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });
});

/** @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AppSidebar } from './AppSidebar';

describe('AppSidebar', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
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

  it('exposes the current page and navigates between dedicated workspaces', async () => {
    const onNavigate = vi.fn();
    await act(async () => root.render(
      <AppSidebar
        activePage="dashboard"
        agentHookCount={2}
        cameraMonitoring={{ enabled: true, connection: 'online' }}
        onNavigate={onNavigate}
      />,
    ));

    const today = container.querySelector<HTMLButtonElement>('button[aria-label="今天"]');
    const camera = container.querySelector<HTMLButtonElement>('button[aria-label="摄像头"]');
    const focus = container.querySelector<HTMLButtonElement>('button[aria-label="番茄钟"]');

    expect(container.querySelector('nav')?.getAttribute('aria-label')).toBe('主导航');
    expect(today?.getAttribute('aria-current')).toBe('page');
    expect(camera?.hasAttribute('aria-current')).toBe(false);
    expect(container.textContent).toContain('2 个 Agent 已连接');
    expect(container.textContent).toContain('摄像头监测中');

    await act(async () => camera?.click());
    expect(onNavigate).toHaveBeenCalledWith('camera');

    await act(async () => focus?.click());
    expect(onNavigate).toHaveBeenCalledWith('focus');
  });
});

import { describe, expect, it, vi } from 'vitest';

import { registerMonitoringWindowGuard } from './monitoringWindowGuard';


describe('registerMonitoringWindowGuard', () => {
  it('minimizes instead of closing only while monitoring is active', () => {
    let closeListener: ((event: { preventDefault(): void }) => void) | undefined;
    let active = false;
    let quitting = false;
    const window = {
      on: vi.fn((_event: string, listener: typeof closeListener) => {
        closeListener = listener;
      }),
      removeListener: vi.fn(),
      minimize: vi.fn(),
    };
    const cleanup = registerMonitoringWindowGuard({
      window,
      isMonitoringActive: () => active,
      isQuitting: () => quitting,
    });
    const event = { preventDefault: vi.fn() };

    closeListener!(event);
    expect(event.preventDefault).not.toHaveBeenCalled();

    active = true;
    closeListener!(event);
    expect(event.preventDefault).toHaveBeenCalledTimes(1);
    expect(window.minimize).toHaveBeenCalledTimes(1);

    quitting = true;
    closeListener!(event);
    expect(event.preventDefault).toHaveBeenCalledTimes(1);

    cleanup();
    expect(window.removeListener).toHaveBeenCalledWith('close', closeListener);
  });
});

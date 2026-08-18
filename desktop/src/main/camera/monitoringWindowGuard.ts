interface CloseEventLike {
  preventDefault(): void;
}

interface WindowLike {
  on(event: 'close', listener: (event: CloseEventLike) => void): unknown;
  removeListener(event: 'close', listener: (event: CloseEventLike) => void): unknown;
  minimize(): void;
}

interface MonitoringWindowGuardOptions {
  window: WindowLike;
  isMonitoringActive(): boolean;
  isQuitting(): boolean;
}

export const registerMonitoringWindowGuard = (
  options: MonitoringWindowGuardOptions,
): (() => void) => {
  const onClose = (event: CloseEventLike): void => {
    if (options.isQuitting() || !options.isMonitoringActive()) return;
    event.preventDefault();
    options.window.minimize();
  };
  options.window.on('close', onClose);
  return () => options.window.removeListener('close', onClose);
};

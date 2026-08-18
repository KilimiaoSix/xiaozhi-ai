import type { XiaofeiDesktopApi } from './shared/contracts';

declare global {
  interface Window {
    xiaofei: XiaofeiDesktopApi;
  }
}

export {};

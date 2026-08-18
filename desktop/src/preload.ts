import { contextBridge } from 'electron';

import type { XiaofeiDesktopApi } from './shared/contracts';

const desktopApi: XiaofeiDesktopApi = {
  getRuntimeInfo: () => ({
    platform: process.platform,
    versions: {
      chrome: process.versions.chrome,
      electron: process.versions.electron,
      node: process.versions.node,
    },
  }),
};

contextBridge.exposeInMainWorld('xiaofei', desktopApi);

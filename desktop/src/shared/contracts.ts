export interface RuntimeInfo {
  platform: NodeJS.Platform;
  versions: {
    chrome: string;
    electron: string;
    node: string;
  };
}

export interface XiaofeiDesktopApi {
  getRuntimeInfo: () => RuntimeInfo;
}

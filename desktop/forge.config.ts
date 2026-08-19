import type { ForgeConfig } from '@electron-forge/shared-types';
import { MakerZIP } from '@electron-forge/maker-zip';
import { VitePlugin } from '@electron-forge/plugin-vite';

const config: ForgeConfig = {
  packagerConfig: {
    appBundleId: 'com.launchcrush.xiaofei',
    asar: true,
    extendInfo: {
      NSCameraUsageDescription: '小智需要使用摄像头来注册主人面部并进行本地人员监测。',
      NSAppleEventsUsageDescription: '用于只读识别 Codex 是否正在等待用户审批。',
    },
    download: {
      mirrorOptions: {
        mirror: 'https://npmmirror.com/mirrors/electron/',
      },
    },
  },
  rebuildConfig: {},
  makers: [new MakerZIP({}, ['darwin'])],
  plugins: [
    new VitePlugin({
      build: [
        {
          entry: 'src/main.ts',
          config: 'vite.main.config.ts',
          target: 'main',
        },
        {
          entry: 'src/preload.ts',
          config: 'vite.preload.config.ts',
          target: 'preload',
        },
      ],
      renderer: [
        {
          name: 'main_window',
          config: 'vite.renderer.config.ts',
        },
      ],
    }),
  ],
};

export default config;

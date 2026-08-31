import { ipcMain, session } from 'electron';

import { CameraStreamClient } from './cameraStreamClient';
import {
  registerCameraStreamIpc,
  type CameraStreamIpcRegistration,
} from './cameraStreamIpc';
import {
  getCameraPermissionStatus,
  openCameraPrivacySettings,
  requestCameraPermission,
} from './cameraPermissions';
import type { AppConfigReader } from '../config/appConfigStore';

interface RegisterCameraIpcOptions {
  /** 摄像头流的地址与令牌来源；serverUrl 变了会断开重连到新地址。 */
  config: AppConfigReader;
}

export const registerCameraIpc = (
  options: RegisterCameraIpcOptions,
): CameraStreamIpcRegistration => {
  const stream = registerCameraStreamIpc({
    ipcMain,
    client: new CameraStreamClient({ config: options.config }),
  });

  ipcMain.handle('camera:get-permission', getCameraPermissionStatus);
  ipcMain.handle('camera:request-permission', requestCameraPermission);
  ipcMain.handle('camera:open-privacy-settings', openCameraPrivacySettings);

  session.defaultSession.setPermissionRequestHandler(
    (_webContents, permission, callback, details) => {
      if (permission !== 'media') {
        callback(false);
        return;
      }
      const mediaTypes = 'mediaTypes' in details ? details.mediaTypes ?? [] : [];
      callback(mediaTypes.includes('video') && !mediaTypes.includes('audio'));
    },
  );

  return {
    isMonitoringActive: stream.isMonitoringActive,
    cleanup: () => {
      stream.cleanup();
      ipcMain.removeHandler('camera:get-permission');
      ipcMain.removeHandler('camera:request-permission');
      ipcMain.removeHandler('camera:open-privacy-settings');
      session.defaultSession.setPermissionRequestHandler(null);
    },
  };
};

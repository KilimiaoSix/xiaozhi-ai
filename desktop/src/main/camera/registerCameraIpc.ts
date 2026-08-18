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

export const registerCameraIpc = (): CameraStreamIpcRegistration => {
  const stream = registerCameraStreamIpc({
    ipcMain,
    client: new CameraStreamClient(),
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

import { ipcMain, session } from 'electron';

import { CameraHttpClient } from './cameraHttpClient';
import {
  getCameraPermissionStatus,
  openCameraPrivacySettings,
  requestCameraPermission,
} from './cameraPermissions';

export const registerCameraIpc = (): void => {
  const client = new CameraHttpClient();

  ipcMain.handle('camera:get-permission', getCameraPermissionStatus);
  ipcMain.handle('camera:request-permission', requestCameraPermission);
  ipcMain.handle('camera:open-privacy-settings', openCameraPrivacySettings);
  ipcMain.handle('camera:enroll-owner', (_event, input) => client.enrollOwner(input));
  ipcMain.handle(
    'camera:upload-frame',
    (_event, input) => client.uploadMonitoringFrame(input),
  );

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
};

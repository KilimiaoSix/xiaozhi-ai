import { shell, systemPreferences } from 'electron';

import type { CameraPermissionStatus } from '../../modules/features/camera-capture/types';

const normalizeStatus = (status: string): CameraPermissionStatus => {
  if (
    status === 'not-determined'
    || status === 'granted'
    || status === 'denied'
    || status === 'restricted'
  ) {
    return status;
  }
  return 'unknown';
};

export const getCameraPermissionStatus = (): CameraPermissionStatus => {
  if (process.platform !== 'darwin') return 'unknown';
  return normalizeStatus(systemPreferences.getMediaAccessStatus('camera'));
};

export const requestCameraPermission = async (): Promise<CameraPermissionStatus> => {
  if (process.platform !== 'darwin') return 'unknown';
  const granted = await systemPreferences.askForMediaAccess('camera');
  return granted ? 'granted' : getCameraPermissionStatus();
};

export const openCameraPrivacySettings = async (): Promise<void> => {
  await shell.openExternal(
    'x-apple.systempreferences:com.apple.preference.security?Privacy_Camera',
  );
};

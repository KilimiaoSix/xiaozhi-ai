export const cameraDesktopGateway = {
  getPermissionStatus: () => window.xiaofei.camera.getPermissionStatus(),
  requestPermission: () => window.xiaofei.camera.requestPermission(),
  openPrivacySettings: () => window.xiaofei.camera.openPrivacySettings(),
};

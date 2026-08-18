import type {
  MonitoringFrameInput,
  OwnerEnrollmentInput,
} from '../types';

export const cameraDesktopGateway = {
  getPermissionStatus: () => window.xiaofei.getCameraPermissionStatus(),
  requestPermission: () => window.xiaofei.requestCameraPermission(),
  openPrivacySettings: () => window.xiaofei.openCameraPrivacySettings(),
  enrollOwner: (input: OwnerEnrollmentInput) => window.xiaofei.enrollOwnerFace(input),
  uploadMonitoringFrame: (input: MonitoringFrameInput) => (
    window.xiaofei.uploadMonitoringFrame(input)
  ),
};

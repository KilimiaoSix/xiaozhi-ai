import type { CameraPermissionStatus } from '../types';

export type CameraMode = 'enrollment' | 'monitoring';

export interface CameraState {
  mode: CameraMode;
  permission: CameraPermissionStatus;
  streamStatus: 'idle' | 'starting' | 'previewing' | 'error';
  enrollment: {
    status: 'idle' | 'captured' | 'uploading' | 'success' | 'error';
    previewUrl: string;
    sampleId: string;
  };
  monitoring: {
    status: 'idle' | 'running' | 'offline' | 'error';
    sentFrames: number;
    droppedFrames: number;
    lastSuccessAt: string;
  };
  errorMessage: string;
}

export const initialCameraState: CameraState = {
  mode: 'enrollment',
  permission: 'unknown',
  streamStatus: 'idle',
  enrollment: {
    status: 'idle',
    previewUrl: '',
    sampleId: '',
  },
  monitoring: {
    status: 'idle',
    sentFrames: 0,
    droppedFrames: 0,
    lastSuccessAt: '',
  },
  errorMessage: '',
};

export type CameraAction =
  | { type: 'mode-selected'; mode: CameraMode }
  | { type: 'permission-updated'; permission: CameraPermissionStatus }
  | { type: 'stream-starting' }
  | { type: 'stream-ready' }
  | { type: 'stream-failed'; message: string }
  | { type: 'enrollment-captured'; previewUrl: string }
  | { type: 'enrollment-retake' }
  | { type: 'enrollment-uploading' }
  | { type: 'enrollment-succeeded'; sampleId: string }
  | { type: 'enrollment-failed'; message: string }
  | { type: 'monitoring-started' }
  | { type: 'monitoring-stopped' }
  | { type: 'monitoring-offline'; message: string }
  | {
    type: 'monitoring-metrics';
    sentFrames: number;
    droppedFrames: number;
    lastSuccessAt: string;
  };

const idleMonitoring = (): CameraState['monitoring'] => ({
  status: 'idle',
  sentFrames: 0,
  droppedFrames: 0,
  lastSuccessAt: '',
});

export const cameraReducer = (
  state: CameraState,
  action: CameraAction,
): CameraState => {
  switch (action.type) {
    case 'mode-selected':
      return {
        ...state,
        mode: action.mode,
        monitoring: action.mode === 'enrollment'
          ? idleMonitoring()
          : state.monitoring,
        errorMessage: '',
      };
    case 'permission-updated':
      return { ...state, permission: action.permission, errorMessage: '' };
    case 'stream-starting':
      return { ...state, streamStatus: 'starting', errorMessage: '' };
    case 'stream-ready':
      return { ...state, streamStatus: 'previewing', errorMessage: '' };
    case 'stream-failed':
      return {
        ...state,
        streamStatus: 'error',
        errorMessage: action.message,
      };
    case 'enrollment-captured':
      return {
        ...state,
        enrollment: {
          status: 'captured',
          previewUrl: action.previewUrl,
          sampleId: '',
        },
        errorMessage: '',
      };
    case 'enrollment-retake':
      return {
        ...state,
        enrollment: { status: 'idle', previewUrl: '', sampleId: '' },
        errorMessage: '',
      };
    case 'enrollment-uploading':
      return {
        ...state,
        enrollment: { ...state.enrollment, status: 'uploading' },
        errorMessage: '',
      };
    case 'enrollment-succeeded':
      return {
        ...state,
        enrollment: {
          ...state.enrollment,
          status: 'success',
          sampleId: action.sampleId,
        },
        errorMessage: '',
      };
    case 'enrollment-failed':
      return {
        ...state,
        enrollment: { ...state.enrollment, status: 'error' },
        errorMessage: action.message,
      };
    case 'monitoring-started':
      return {
        ...state,
        monitoring: { ...idleMonitoring(), status: 'running' },
        errorMessage: '',
      };
    case 'monitoring-stopped':
      return { ...state, monitoring: idleMonitoring(), errorMessage: '' };
    case 'monitoring-offline':
      return {
        ...state,
        monitoring: { ...state.monitoring, status: 'offline' },
        errorMessage: action.message,
      };
    case 'monitoring-metrics':
      return {
        ...state,
        monitoring: {
          status: 'running',
          sentFrames: action.sentFrames,
          droppedFrames: action.droppedFrames,
          lastSuccessAt: action.lastSuccessAt,
        },
        errorMessage: '',
      };
  }
};

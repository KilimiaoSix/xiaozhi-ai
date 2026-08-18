export type CameraPermissionStatus =
  | 'not-determined'
  | 'granted'
  | 'denied'
  | 'restricted'
  | 'unknown';

export type RecognitionStreamMode = 'monitoring' | 'enrollment';

export interface RecognitionStreamOptions {
  mode: RecognitionStreamMode;
  workstationId: string;
  displayName?: string;
}

export type FrameSendResult = 'sent' | 'dropped' | 'not-ready';

export interface RecognitionEvent {
  type: string;
  session_id?: string | null;
  sequence?: number;
  [key: string]: unknown;
}

export type RecognitionConnectionState =
  | 'idle'
  | 'connecting'
  | 'online'
  | 'reconnecting';

export type PresenceRecognitionStatus =
  | 'starting'
  | 'present'
  | 'absent'
  | 'camera_error'
  | 'stale';

export type FaceRecognitionStatus =
  | 'starting'
  | 'not_enrolled'
  | 'no_face'
  | 'owner'
  | 'unknown'
  | 'multiple_faces'
  | 'camera_error';

export interface PresenceRecognitionState {
  state: PresenceRecognitionStatus;
  changed: boolean;
}

export interface FaceRecognitionState {
  state: FaceRecognitionStatus;
  faceCount: number;
  faceDetected: boolean;
  matched: boolean;
  similarity?: number;
  threshold?: number;
}

export interface RecognitionMetrics {
  sentFrames: number;
  clientDropped: number;
  processedFrames: number;
  serverDropped: number;
  lastResultAt: string;
}

export interface EnrollmentState {
  status: 'idle' | 'starting' | 'running' | 'success' | 'error';
  accepted: number;
  required: number;
  reason: string;
  sampleId: string;
  sampleCount: number;
  storedAt: string;
}

export type CameraPermissionStatus =
  | 'not-determined'
  | 'granted'
  | 'denied'
  | 'restricted'
  | 'unknown';

export interface OwnerEnrollmentInput {
  displayName: string;
  capturedAt: string;
  jpeg: ArrayBuffer;
}

export interface OwnerEnrollmentResult {
  profileId: string;
  sampleId: string;
  storedAt: string;
}

export interface MonitoringFrameInput {
  sessionId: string;
  sequence: number;
  capturedAt: string;
  jpeg: ArrayBuffer;
}

export interface MonitoringFrameResult {
  accepted: boolean;
  sessionId: string;
  sequence: number;
  receivedAt: string;
}

export type CameraGatewayErrorCode =
  | 'http-error'
  | 'invalid-response'
  | 'offline'
  | 'timeout';

export class CameraGatewayError extends Error {
  constructor(
    public readonly code: CameraGatewayErrorCode,
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'CameraGatewayError';
  }
}

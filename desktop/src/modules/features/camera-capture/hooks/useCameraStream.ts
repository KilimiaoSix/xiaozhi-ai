import { useCallback, useEffect, useRef, useState } from 'react';

const cameraErrorMessage = (error: unknown): string => {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError') return '摄像头权限未开启';
    if (error.name === 'NotFoundError') return '没有检测到可用摄像头';
    if (error.name === 'NotReadableError') return '摄像头正被其他应用占用';
    if (error.name === 'OverconstrainedError') return '选择的摄像头当前不可用';
  }
  return error instanceof Error ? error.message : '摄像头启动失败';
};

export interface CameraStreamController {
  stream: MediaStream | null;
  devices: MediaDeviceInfo[];
  selectedDeviceId: string;
  errorMessage: string;
  start: (deviceId?: string) => Promise<void>;
  stop: () => void;
}

export const useCameraStream = (): CameraStreamController => {
  const streamRef = useRef<MediaStream | null>(null);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState('');
  const [errorMessage, setErrorMessage] = useState('');

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    setStream(null);
  }, []);

  const start = useCallback(async (deviceId?: string) => {
    stop();
    setErrorMessage('');
    try {
      const nextStream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          deviceId: deviceId ? { exact: deviceId } : undefined,
          width: { ideal: 1280 },
          height: { ideal: 720 },
          facingMode: 'user',
        },
      });
      streamRef.current = nextStream;
      setStream(nextStream);

      const videoDevices = (await navigator.mediaDevices.enumerateDevices())
        .filter((device) => device.kind === 'videoinput');
      setDevices(videoDevices);
      const activeDeviceId = nextStream.getVideoTracks()[0]?.getSettings().deviceId;
      setSelectedDeviceId(activeDeviceId ?? deviceId ?? videoDevices[0]?.deviceId ?? '');
    } catch (error) {
      const message = cameraErrorMessage(error);
      setErrorMessage(message);
      throw new Error(message);
    }
  }, [stop]);

  useEffect(() => stop, [stop]);

  return {
    stream,
    devices,
    selectedDeviceId,
    errorMessage,
    start,
    stop,
  };
};

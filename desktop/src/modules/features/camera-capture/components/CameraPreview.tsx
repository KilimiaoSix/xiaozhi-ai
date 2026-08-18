import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
} from 'react';

export interface CameraPreviewHandle {
  captureJpeg: () => Promise<ArrayBuffer>;
}

interface CameraPreviewProps {
  stream: MediaStream | null;
  capturedUrl: string;
  activeLabel: string;
  monitoring: boolean;
}

export const captureJpeg = async (
  video: HTMLVideoElement,
): Promise<ArrayBuffer> => {
  if (!video.videoWidth || !video.videoHeight) {
    throw new Error('摄像头画面尚未准备好');
  }
  const scale = Math.min(1, 1280 / video.videoWidth, 720 / video.videoHeight);
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
  canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
  const context = canvas.getContext('2d', { alpha: false });
  if (!context) throw new Error('无法读取摄像头画面');
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (value) => value
        ? resolve(value)
        : reject(new Error('JPEG 编码失败')),
      'image/jpeg',
      0.82,
    );
  });
  return blob.arrayBuffer();
};

export const CameraPreview = forwardRef<
  CameraPreviewHandle,
  CameraPreviewProps
>(function CameraPreview({ stream, capturedUrl, activeLabel, monitoring }, ref) {
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.srcObject = stream;
    if (stream) void video.play();
    return () => {
      video.srcObject = null;
    };
  }, [stream]);

  useImperativeHandle(ref, () => ({
    captureJpeg: () => {
      if (!videoRef.current) throw new Error('摄像头预览不可用');
      return captureJpeg(videoRef.current);
    },
  }), []);

  return (
    <div className="camera-preview">
      <video
        ref={videoRef}
        autoPlay
        muted
        playsInline
        hidden={Boolean(capturedUrl)}
      />
      {capturedUrl && <img src={capturedUrl} alt="待确认的主人照片" />}
      {!stream && !capturedUrl && (
        <div className="camera-preview-empty">
          <span className="camera-empty-icon" aria-hidden="true">◉</span>
          <strong>摄像头尚未启用</strong>
          <small>启用后画面只会发送给这台 Mac 上的 Server</small>
        </div>
      )}
      {(stream || capturedUrl) && (
        <>
          <span className="camera-usage-chip">
            <i />{activeLabel}
          </span>
          {monitoring && <span className="camera-fps-chip">1 FPS · JPEG</span>}
          {!monitoring && !capturedUrl && <span className="camera-face-guide" />}
        </>
      )}
    </div>
  );
});

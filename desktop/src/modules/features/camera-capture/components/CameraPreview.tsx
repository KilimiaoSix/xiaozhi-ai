import { useEffect, useRef } from 'react';

interface CameraPreviewProps {
  stream: MediaStream | null;
  activeLabel: string;
  monitoring: boolean;
  enrollment: boolean;
}

export function CameraPreview({
  stream,
  activeLabel,
  monitoring,
  enrollment,
}: CameraPreviewProps) {
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.srcObject = stream;
    if (stream) void video.play();
    return () => { video.srcObject = null; };
  }, [stream]);

  return (
    <div className="camera-preview">
      <video ref={videoRef} autoPlay muted playsInline />
      {!stream && (
        <div className="camera-preview-empty">
          <span className="camera-empty-icon" aria-hidden="true">◉</span>
          <strong>摄像头尚未启用</strong>
          <small>开启监测或主人录入后，画面只发送给本地 Server</small>
        </div>
      )}
      {stream && (
        <>
          <span className="camera-usage-chip"><i />{activeLabel}</span>
          <span className="camera-fps-chip">5 FPS · JPEG</span>
          {enrollment && <span className="camera-face-guide" />}
        </>
      )}
      {monitoring && !stream && (
        <span className="camera-usage-chip"><i />摄像头恢复中</span>
      )}
    </div>
  );
}

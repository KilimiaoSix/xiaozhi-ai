import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';

import { CameraPreview, type CameraPreviewHandle } from './components/CameraPreview';
import { OwnerEnrollment } from './components/OwnerEnrollment';
import { PresenceMonitoring } from './components/PresenceMonitoring';
import { useCameraStream } from './hooks/useCameraStream';
import { cameraDesktopGateway } from './services/cameraDesktopGateway';
import { FrameUploadScheduler } from './services/frameUploadScheduler';
import { cameraReducer, initialCameraState } from './state/cameraReducer';
import './camera.css';

const timeLabel = (): string => new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
}).format(new Date());

interface CameraPageProps {
  onNavigateHome?: () => void;
}

export function CameraPage({ onNavigateHome }: CameraPageProps) {
  const [state, dispatch] = useReducer(cameraReducer, initialCameraState);
  const [displayName, setDisplayName] = useState('主人');
  const [capturedJpeg, setCapturedJpeg] = useState<ArrayBuffer | null>(null);
  const [serverConnected, setServerConnected] = useState<boolean | null>(null);
  const previewRef = useRef<CameraPreviewHandle>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const schedulerRef = useRef<FrameUploadScheduler | null>(null);
  const sessionIdRef = useRef('');
  const sequenceRef = useRef(0);
  const lastSuccessRef = useRef('');
  const camera = useCameraStream();

  const monitoringActive = state.monitoring.status !== 'idle';

  const releaseCapturedUrl = useCallback(() => {
    if (state.enrollment.previewUrl) URL.revokeObjectURL(state.enrollment.previewUrl);
  }, [state.enrollment.previewUrl]);

  const stopMonitoring = useCallback(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    intervalRef.current = null;
    schedulerRef.current?.stop();
    schedulerRef.current = null;
    camera.stop();
    dispatch({ type: 'monitoring-stopped' });
  }, [camera.stop]);

  useEffect(() => () => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    schedulerRef.current?.stop();
    releaseCapturedUrl();
  }, [releaseCapturedUrl]);

  useEffect(() => {
    void cameraDesktopGateway.getPermissionStatus().then((permission) => {
      dispatch({ type: 'permission-updated', permission });
    });
  }, []);

  const enableCamera = useCallback(async (): Promise<boolean> => {
    dispatch({ type: 'stream-starting' });
    try {
      let permission = state.permission;
      if (permission !== 'granted' && permission !== 'unknown') {
        permission = await cameraDesktopGateway.requestPermission();
        dispatch({ type: 'permission-updated', permission });
      }
      if (permission === 'denied' || permission === 'restricted') {
        throw new Error('摄像头权限未开启');
      }
      await camera.start();
      dispatch({ type: 'stream-ready' });
      return true;
    } catch (error) {
      dispatch({
        type: 'stream-failed',
        message: error instanceof Error ? error.message : '摄像头启动失败',
      });
      return false;
    }
  }, [camera.start, state.permission]);

  const selectMode = (mode: 'enrollment' | 'monitoring'): void => {
    if (monitoringActive) stopMonitoring();
    if (mode === 'monitoring' && state.enrollment.previewUrl) {
      releaseCapturedUrl();
      setCapturedJpeg(null);
      dispatch({ type: 'enrollment-retake' });
    }
    dispatch({ type: 'mode-selected', mode });
  };

  const captureOwner = async (): Promise<void> => {
    try {
      const jpeg = await previewRef.current?.captureJpeg();
      if (!jpeg) throw new Error('摄像头预览不可用');
      releaseCapturedUrl();
      setCapturedJpeg(jpeg);
      dispatch({
        type: 'enrollment-captured',
        previewUrl: URL.createObjectURL(new Blob([jpeg], { type: 'image/jpeg' })),
      });
      camera.stop();
    } catch (error) {
      dispatch({
        type: 'enrollment-failed',
        message: error instanceof Error ? error.message : '拍照失败',
      });
    }
  };

  const retakeOwner = (): void => {
    releaseCapturedUrl();
    setCapturedJpeg(null);
    dispatch({ type: 'enrollment-retake' });
    void enableCamera();
  };

  const uploadOwner = async (): Promise<void> => {
    if (!capturedJpeg) return;
    dispatch({ type: 'enrollment-uploading' });
    try {
      const result = await cameraDesktopGateway.enrollOwner({
        displayName,
        capturedAt: new Date().toISOString(),
        jpeg: capturedJpeg,
      });
      setServerConnected(true);
      dispatch({ type: 'enrollment-succeeded', sampleId: result.sampleId });
    } catch (error) {
      setServerConnected(false);
      dispatch({
        type: 'enrollment-failed',
        message: error instanceof Error ? error.message : '主人照片上传失败',
      });
    }
  };

  const startMonitoring = useCallback(async () => {
    if (!camera.stream && !await enableCamera()) return;

    sessionIdRef.current = crypto.randomUUID();
    sequenceRef.current = 0;
    lastSuccessRef.current = '';
    const scheduler = new FrameUploadScheduler(async (jpeg) => {
      sequenceRef.current += 1;
      await cameraDesktopGateway.uploadMonitoringFrame({
        sessionId: sessionIdRef.current,
        sequence: sequenceRef.current,
        capturedAt: new Date().toISOString(),
        jpeg,
      });
    });
    schedulerRef.current = scheduler;
    dispatch({ type: 'monitoring-started' });

    const sendFrame = async (): Promise<void> => {
      try {
        const jpeg = await previewRef.current?.captureJpeg();
        if (!jpeg) return;
        const result = await scheduler.submit(jpeg);
        const snapshot = scheduler.snapshot();
        if (result === 'sent') {
          lastSuccessRef.current = timeLabel();
          setServerConnected(true);
        }
        dispatch({
          type: 'monitoring-metrics',
          sentFrames: snapshot.sent,
          droppedFrames: snapshot.dropped,
          lastSuccessAt: lastSuccessRef.current,
        });
      } catch (error) {
        const snapshot = scheduler.snapshot();
        setServerConnected(false);
        dispatch({
          type: 'monitoring-metrics',
          sentFrames: snapshot.sent,
          droppedFrames: snapshot.dropped,
          lastSuccessAt: lastSuccessRef.current,
        });
        dispatch({
          type: 'monitoring-offline',
          message: error instanceof Error ? error.message : '监测帧上传失败',
        });
      }
    };

    intervalRef.current = setInterval(() => void sendFrame(), 1000);
  }, [camera.stream, enableCamera]);

  const toggleMonitoring = (): void => {
    if (monitoringActive) stopMonitoring();
    else void startMonitoring();
  };

  const cameraName = useMemo(() => {
    const selected = camera.devices.find(
      (device) => device.deviceId === camera.selectedDeviceId,
    );
    return selected?.label || 'Mac 摄像头';
  }, [camera.devices, camera.selectedDeviceId]);

  return (
    <div className="camera-shell">
      <div className="camera-titlebar" aria-hidden="true"><span>小飞</span></div>
      <aside className="camera-sidebar">
        <div className="camera-brand">小飞</div>
        <nav aria-label="主导航">
          <button type="button" onClick={onNavigateHome}><i>今</i>今天</button>
          <button className="is-active" type="button"><i>像</i>摄像头</button>
          <button type="button"><i>连</i>连接</button>
          <button type="button"><i>设</i>设置</button>
        </nav>
        <div className="camera-sidebar-foot">
          <i className={serverConnected === false ? 'is-offline' : ''} />
          {serverConnected === null
            ? '本地 Server'
            : serverConnected ? 'Server 已连接' : 'Server 不可用'}
        </div>
      </aside>

      <main className="camera-content">
        <header className="camera-page-heading">
          <div><h1>摄像头</h1><p>{cameraName}</p></div>
          {camera.devices.length > 1 && (
            <label className="camera-device-select">
              <span>摄像头</span>
              <select
                value={camera.selectedDeviceId}
                onChange={(event) => void camera.start(event.target.value)}
              >
                {camera.devices.map((device, index) => (
                  <option key={device.deviceId} value={device.deviceId}>
                    {device.label || `摄像头 ${index + 1}`}
                  </option>
                ))}
              </select>
            </label>
          )}
        </header>

        <div className="camera-segmented" role="tablist" aria-label="采集模式">
          <button
            className={state.mode === 'enrollment' ? 'is-active' : ''}
            type="button"
            role="tab"
            aria-selected={state.mode === 'enrollment'}
            onClick={() => selectMode('enrollment')}
          >
            主人录入
          </button>
          <button
            className={state.mode === 'monitoring' ? 'is-active' : ''}
            type="button"
            role="tab"
            aria-selected={state.mode === 'monitoring'}
            onClick={() => selectMode('monitoring')}
          >
            实时监测
          </button>
        </div>

        <CameraPreview
          ref={previewRef}
          stream={camera.stream}
          capturedUrl={state.enrollment.previewUrl}
          activeLabel={monitoringActive
            ? '监测中'
            : state.enrollment.previewUrl
              ? '照片已拍摄 · 摄像头已关闭'
              : '摄像头使用中'}
          monitoring={monitoringActive}
        />

        {state.mode === 'enrollment' ? (
          <OwnerEnrollment
            displayName={displayName}
            status={state.enrollment.status}
            sampleId={state.enrollment.sampleId}
            cameraReady={Boolean(camera.stream)}
            onDisplayNameChange={setDisplayName}
            onEnableCamera={() => void enableCamera()}
            onCapture={() => void captureOwner()}
            onRetake={retakeOwner}
            onUpload={() => void uploadOwner()}
          />
        ) : (
          <PresenceMonitoring
            active={monitoringActive}
            sentFrames={state.monitoring.sentFrames}
            droppedFrames={state.monitoring.droppedFrames}
            lastSuccessAt={state.monitoring.lastSuccessAt}
            offline={state.monitoring.status === 'offline'}
            onToggle={toggleMonitoring}
          />
        )}

        {(state.errorMessage || camera.errorMessage) && (
          <div className="camera-error-banner" role="alert">
            <div>
              <strong>当前操作没有完成</strong>
              <span>{state.errorMessage || camera.errorMessage}</span>
            </div>
            {(state.permission === 'denied' || state.permission === 'restricted') && (
              <button
                type="button"
                onClick={() => void cameraDesktopGateway.openPrivacySettings()}
              >
                打开系统设置
              </button>
            )}
          </div>
        )}
      </main>
    </div>
  );
}

import { useEffect, useMemo, useRef, useState } from 'react';

import { CameraPreview } from './components/CameraPreview';
import { OwnerEnrollment } from './components/OwnerEnrollment';
import { PresenceMonitoring } from './components/PresenceMonitoring';
import { useCameraMonitoring } from './context/CameraMonitoringProvider';
import { cameraDesktopGateway } from './services/cameraDesktopGateway';
import './camera.css';

interface CameraPageProps {
  onNavigateHome?: () => void;
}

export function CameraPage({ onNavigateHome }: CameraPageProps) {
  const camera = useCameraMonitoring();
  const [mode, setMode] = useState<'enrollment' | 'monitoring'>(
    camera.enabled ? 'monitoring' : 'enrollment',
  );
  const [displayName, setDisplayName] = useState('主人');
  const enrollmentActiveRef = useRef(false);

  const enrollmentActive = camera.enrollment.status === 'starting'
    || camera.enrollment.status === 'running';
  enrollmentActiveRef.current = enrollmentActive;

  useEffect(() => {
    if (camera.enabled) setMode('monitoring');
  }, [camera.enabled]);

  useEffect(() => () => {
    if (enrollmentActiveRef.current) void camera.cancelEnrollment();
  }, [camera.cancelEnrollment]);

  const cameraName = useMemo(() => {
    const selected = camera.devices.find(
      (device) => device.deviceId === camera.selectedDeviceId,
    );
    return selected?.label || 'Mac 摄像头';
  }, [camera.devices, camera.selectedDeviceId]);

  const toggleMonitoring = (): void => {
    if (camera.enabled) void camera.stopMonitoring();
    else void camera.startMonitoring();
  };

  const navigateHome = (): void => {
    if (enrollmentActive) void camera.cancelEnrollment();
    onNavigateHome?.();
  };

  const serverConnected = camera.connection === 'online';
  const serverWaiting = camera.connection === 'connecting'
    || camera.connection === 'reconnecting';

  return (
    <div className="camera-shell">
      <div className="camera-titlebar" aria-hidden="true"><span>小飞</span></div>
      <aside className="camera-sidebar">
        <div className="camera-brand">小飞</div>
        <nav aria-label="主导航">
          <button type="button" onClick={navigateHome}><i>今</i>今天</button>
          <button className="is-active" type="button"><i>像</i>摄像头</button>
          <button type="button"><i>连</i>连接</button>
          <button type="button"><i>设</i>设置</button>
        </nav>
        <div className="camera-sidebar-foot">
          <i className={!serverConnected && camera.enabled ? 'is-offline' : ''} />
          {!camera.enabled
            ? '本地 Server'
            : serverConnected
              ? 'Server 已连接'
              : serverWaiting ? '等待 Server' : 'Server 不可用'}
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
                onChange={(event) => void camera.selectDevice(event.target.value)}
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
            className={mode === 'enrollment' ? 'is-active' : ''}
            type="button"
            role="tab"
            aria-selected={mode === 'enrollment'}
            disabled={camera.enabled}
            title={camera.enabled ? '请先关闭实时监测' : undefined}
            onClick={() => setMode('enrollment')}
          >
            主人录入
          </button>
          <button
            className={mode === 'monitoring' ? 'is-active' : ''}
            type="button"
            role="tab"
            aria-selected={mode === 'monitoring'}
            disabled={enrollmentActive}
            title={enrollmentActive ? '请先取消主人录入' : undefined}
            onClick={() => setMode('monitoring')}
          >
            实时监测
          </button>
        </div>

        <CameraPreview
          stream={camera.stream}
          activeLabel={camera.enabled
            ? '监测中'
            : enrollmentActive ? '主人录入中' : '摄像头使用中'}
          monitoring={camera.enabled}
          enrollment={enrollmentActive}
        />

        {mode === 'enrollment' ? (
          <OwnerEnrollment
            displayName={displayName}
            enrollment={camera.enrollment}
            disabled={camera.enabled}
            onDisplayNameChange={setDisplayName}
            onStart={() => void camera.startEnrollment(displayName)}
            onCancel={() => void camera.cancelEnrollment()}
            onReset={() => void camera.cancelEnrollment()}
          />
        ) : (
          <PresenceMonitoring
            enabled={camera.enabled}
            connection={camera.connection}
            presence={camera.presence}
            identity={camera.identity}
            metrics={camera.metrics}
            onToggle={toggleMonitoring}
          />
        )}

        {camera.errorMessage && (
          <div className="camera-error-banner" role="alert">
            <div>
              <strong>当前操作没有完成</strong>
              <span>{camera.errorMessage}</span>
            </div>
            {camera.errorMessage.includes('权限') && (
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

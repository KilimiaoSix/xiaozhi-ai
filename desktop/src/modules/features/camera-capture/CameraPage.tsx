import { useEffect, useMemo, useRef, useState } from 'react';
import { Camera, ScanFace, ScanLine, Settings, TriangleAlert } from 'lucide-react';

import { CameraPreview } from './components/CameraPreview';
import { OwnerEnrollment } from './components/OwnerEnrollment';
import { PresenceMonitoring } from './components/PresenceMonitoring';
import { useCameraMonitoring } from './context/CameraMonitoringProvider';
import { cameraDesktopGateway } from './services/cameraDesktopGateway';
import { wellbeingDesktopGateway } from './services/wellbeingDesktopGateway';
import type { WellbeingTestKind } from '../wellbeing/contracts';
import './camera.css';

export function CameraPage() {
  const camera = useCameraMonitoring();
  const [mode, setMode] = useState<'enrollment' | 'monitoring'>(
    camera.enabled ? 'monitoring' : 'enrollment',
  );
  const [displayName, setDisplayName] = useState('主人');
  const [testingWellbeingKind, setTestingWellbeingKind] =
    useState<WellbeingTestKind | null>(null);
  const [wellbeingTestMessage, setWellbeingTestMessage] = useState('');
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

  const testWellbeing = async (kind: WellbeingTestKind): Promise<void> => {
    if (testingWellbeingKind !== null) return;
    setTestingWellbeingKind(kind);
    setWellbeingTestMessage('');
    try {
      await wellbeingDesktopGateway.sendTest(kind);
      setWellbeingTestMessage('测试提醒已发送');
    } catch (error) {
      setWellbeingTestMessage(
        error instanceof Error ? error.message : '测试提醒发送失败',
      );
    } finally {
      setTestingWellbeingKind(null);
    }
  };

  return (
    <div className="camera-shell">
      <main className="camera-content">
        <header className="camera-page-heading">
          <div>
            <span className="camera-heading-icon" aria-hidden="true"><Camera size={20} /></span>
            <div><h1>摄像头</h1><p>{cameraName}</p></div>
          </div>
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
            <ScanFace size={15} aria-hidden="true" />主人录入
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
            <ScanLine size={15} aria-hidden="true" />实时监测
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
            onTest={(kind) => void testWellbeing(kind)}
            testingKind={testingWellbeingKind}
            testMessage={wellbeingTestMessage}
          />
        )}

        {camera.errorMessage && (
          <div className="camera-error-banner" role="alert">
            <TriangleAlert size={18} aria-hidden="true" />
            <div>
              <strong>当前操作没有完成</strong>
              <span>{camera.errorMessage}</span>
            </div>
            {camera.errorMessage.includes('权限') && (
              <button
                type="button"
                onClick={() => void cameraDesktopGateway.openPrivacySettings()}
              >
                <Settings size={14} aria-hidden="true" />打开系统设置
              </button>
            )}
          </div>
        )}
      </main>
    </div>
  );
}

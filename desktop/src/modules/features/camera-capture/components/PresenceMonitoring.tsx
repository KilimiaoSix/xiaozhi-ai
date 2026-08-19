import type {
  FaceRecognitionState,
  PresenceRecognitionState,
  RecognitionConnectionState,
  RecognitionMetrics,
} from '../types';
import type { WellbeingTestKind } from '../../wellbeing/contracts';
import { FlaskConical, ShieldCheck } from 'lucide-react';

interface PresenceMonitoringProps {
  enabled: boolean;
  connection: RecognitionConnectionState;
  presence: PresenceRecognitionState;
  identity: FaceRecognitionState;
  metrics: RecognitionMetrics;
  onToggle: () => void;
  onTest: (kind: WellbeingTestKind) => void;
  testingKind: WellbeingTestKind | null;
  testMessage: string;
}

const testActions: Array<{ kind: WellbeingTestKind; label: string }> = [
  { kind: 'long_work', label: '测试久坐提醒' },
  { kind: 'commute_safety', label: '测试通勤安全' },
  { kind: 'overtime', label: '测试 21 点提醒' },
  { kind: 'frantic_overtime', label: '测试深夜强提醒' },
  { kind: 'warm_encouragement', label: '测试暖心加油' },
];

const connectionLabels: Record<RecognitionConnectionState, string> = {
  idle: '已停止',
  connecting: '连接中',
  online: '监测中',
  reconnecting: '重连中',
};

const presenceLabels: Record<PresenceRecognitionState['state'], string> = {
  starting: '判断中',
  present: '有人',
  absent: '无人',
  camera_error: '摄像头异常',
  stale: '结果已过期',
};

const identityLabels: Record<FaceRecognitionState['state'], string> = {
  starting: '判断中',
  not_enrolled: '尚未注册主人',
  no_face: '未检测到人脸',
  owner: '主人',
  unknown: '陌生人',
  multiple_faces: '多张人脸',
  camera_error: '摄像头异常',
};

const resultTime = (value: string): string => {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
};

export function PresenceMonitoring({
  enabled,
  connection,
  presence,
  identity,
  metrics,
  onToggle,
  onTest,
  testingKind,
  testMessage,
}: PresenceMonitoringProps) {
  return (
    <section className="camera-control-card" aria-label="实时监测">
      <div className="camera-card-heading">
        <div>
          <strong>人员监测</strong>
          <span>持续发送内存中的 JPEG 帧，不录制视频</span>
        </div>
        <button
          className={`camera-switch ${enabled ? 'is-on' : ''}`}
          type="button"
          role="switch"
          aria-label="实时监测"
          aria-checked={enabled}
          onClick={onToggle}
        >
          <span />
        </button>
      </div>

      <div className="camera-recognition-summary">
        <div>
          <span>人体</span>
          <strong>{enabled ? presenceLabels[presence.state] : '未监测'}</strong>
        </div>
        <div>
          <span>人脸</span>
          <strong>{enabled && identity.faceDetected ? '检测到人脸' : '未检测到人脸'}</strong>
        </div>
        <div>
          <span>身份</span>
          <strong>{enabled ? identityLabels[identity.state] : '未监测'}</strong>
        </div>
        <div>
          <span>匹配结果</span>
          <strong className={identity.matched ? 'is-owner' : ''}>
            {enabled && identity.matched ? '已匹配' : '未匹配'}
          </strong>
        </div>
        {identity.similarity !== undefined && (
          <div>
            <span>相似度</span>
            <strong>{(identity.similarity * 100).toFixed(1)}%</strong>
          </div>
        )}
      </div>

      <div className="camera-metrics">
        <div>
          <span>连接状态</span>
          <strong className={connection === 'reconnecting' ? 'is-warning' : ''}>
            {enabled ? connectionLabels[connection] : '已停止'}
          </strong>
        </div>
        <div><span>客户端发送</span><strong>{metrics.sentFrames}</strong></div>
        <div><span>Server 处理</span><strong>{metrics.processedFrames}</strong></div>
        <div><span>客户端丢弃</span><strong>{metrics.clientDropped}</strong></div>
        <div><span>Server 丢弃</span><strong>{metrics.serverDropped}</strong></div>
        <div><span>最近结果</span><strong>{resultTime(metrics.lastResultAt)}</strong></div>
      </div>

      <div className="camera-test-action">
        <div className="camera-test-buttons">
          {testActions.map(({ kind, label }) => (
            <button
              key={kind}
              type="button"
              disabled={testingKind !== null}
              aria-busy={testingKind === kind}
              onClick={() => onTest(kind)}
            >
              <FlaskConical size={14} aria-hidden="true" />
              {testingKind === kind ? '发送中…' : label}
            </button>
          ))}
        </div>
        <span role="status">{testMessage}</span>
      </div>

      <p className="camera-privacy-note">
        <ShieldCheck size={16} aria-hidden="true" />
        原始监测帧只在内存中流转，Server 不会保存。
      </p>
    </section>
  );
}

interface PresenceMonitoringProps {
  active: boolean;
  sentFrames: number;
  droppedFrames: number;
  lastSuccessAt: string;
  offline: boolean;
  onToggle: () => void;
}

export function PresenceMonitoring({
  active,
  sentFrames,
  droppedFrames,
  lastSuccessAt,
  offline,
  onToggle,
}: PresenceMonitoringProps) {
  return (
    <section className="camera-control-card" aria-label="实时监测">
      <div className="camera-card-heading">
        <div>
          <strong>人员监测</strong>
          <span>每秒发送一张 JPEG，不录制视频</span>
        </div>
        <button
          className={`camera-switch ${active ? 'is-on' : ''}`}
          type="button"
          role="switch"
          aria-checked={active}
          onClick={onToggle}
        >
          <span />
        </button>
      </div>

      <div className="camera-metrics">
        <div>
          <span>传输状态</span>
          <strong className={offline ? 'is-warning' : ''}>
            {!active ? '已停止' : offline ? '等待 Server' : '稳定'}
          </strong>
        </div>
        <div><span>已发送</span><strong>{sentFrames}</strong></div>
        <div><span>已丢弃</span><strong>{droppedFrames}</strong></div>
        <div><span>最近成功</span><strong>{lastSuccessAt || '—'}</strong></div>
      </div>

      <p className="camera-privacy-note">
        <i aria-hidden="true">✓</i>
        原始监测帧只在内存中流转，Server 不会保存。
      </p>
    </section>
  );
}

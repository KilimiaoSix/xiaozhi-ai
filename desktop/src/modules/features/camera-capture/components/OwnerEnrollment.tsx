import type { EnrollmentState } from '../types';

interface OwnerEnrollmentProps {
  displayName: string;
  enrollment: EnrollmentState;
  disabled: boolean;
  onDisplayNameChange: (value: string) => void;
  onStart: () => void;
  onCancel: () => void;
  onReset: () => void;
}

const reasonLabels: Record<string, string> = {
  accepted: '样本已接受，请保持自然姿态',
  sample_too_soon: '请稳定保持，正在采集下一帧',
  no_face: '未检测到人脸，请正对摄像头',
  multiple_faces: '画面中请只保留一张人脸',
  face_too_small: '请靠近摄像头一些',
  blurry: '画面不够清晰，请保持稳定',
  low_quality: '当前画面质量不足，请调整光线',
};

export function OwnerEnrollment({
  displayName,
  enrollment,
  disabled,
  onDisplayNameChange,
  onStart,
  onCancel,
  onReset,
}: OwnerEnrollmentProps) {
  const active = enrollment.status === 'starting' || enrollment.status === 'running';

  return (
    <section className="camera-control-card" aria-label="主人录入">
      <div className="camera-card-heading">
        <div>
          <strong>注册主人</strong>
          <span>连续采集 20 个合格样本，原始画面不会保存</span>
        </div>
        {enrollment.status === 'success' && (
          <span className="camera-success-badge">已保存</span>
        )}
      </div>

      <label className="camera-field">
        <span>主人名称</span>
        <input
          value={displayName}
          onChange={(event) => onDisplayNameChange(event.target.value)}
          placeholder="输入称呼"
          maxLength={64}
          disabled={active}
        />
      </label>

      {active && (
        <div className="camera-enrollment-progress" aria-live="polite">
          <div>
            <span>有效样本</span>
            <strong>{enrollment.accepted} / {enrollment.required}</strong>
          </div>
          <progress value={enrollment.accepted} max={enrollment.required} />
          <p>{reasonLabels[enrollment.reason] ?? '正在初始化识别模型'}</p>
        </div>
      )}

      {disabled && !active && (
        <p className="camera-inline-warning">请先关闭实时监测，再开始主人录入。</p>
      )}

      <div className="camera-actions">
        {active ? (
          <button className="camera-secondary-button" type="button" onClick={onCancel}>
            取消注册
          </button>
        ) : enrollment.status === 'success' ? (
          <button className="camera-secondary-button" type="button" onClick={onReset}>
            重新录入
          </button>
        ) : (
          <button
            className="camera-primary-button"
            type="button"
            onClick={onStart}
            disabled={disabled || !displayName.trim()}
          >
            开始注册
          </button>
        )}
      </div>

      {enrollment.status === 'success' && (
        <div className="camera-enrollment-result">
          <span>{enrollment.sampleCount} 个有效样本已生成主人模板</span>
          <code>{enrollment.sampleId}</code>
        </div>
      )}
    </section>
  );
}

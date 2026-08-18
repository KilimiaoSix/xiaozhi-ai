interface OwnerEnrollmentProps {
  displayName: string;
  status: 'idle' | 'captured' | 'uploading' | 'success' | 'error';
  sampleId: string;
  cameraReady: boolean;
  onDisplayNameChange: (value: string) => void;
  onEnableCamera: () => void;
  onCapture: () => void;
  onRetake: () => void;
  onUpload: () => void;
}

export function OwnerEnrollment({
  displayName,
  status,
  sampleId,
  cameraReady,
  onDisplayNameChange,
  onEnableCamera,
  onCapture,
  onRetake,
  onUpload,
}: OwnerEnrollmentProps) {
  const hasCapture = status !== 'idle';

  return (
    <section className="camera-control-card" aria-label="主人录入">
      <div className="camera-card-heading">
        <div>
          <strong>注册主人</strong>
          <span>只在确认后保存这张照片</span>
        </div>
        {status === 'success' && <span className="camera-success-badge">已保存</span>}
      </div>

      <label className="camera-field">
        <span>主人名称</span>
        <input
          value={displayName}
          onChange={(event) => onDisplayNameChange(event.target.value)}
          placeholder="输入称呼"
          maxLength={64}
        />
      </label>

      <div className="camera-actions">
        {!cameraReady && status === 'idle' && (
          <button className="camera-primary-button" type="button" onClick={onEnableCamera}>
            启用摄像头
          </button>
        )}
        {cameraReady && status === 'idle' && (
          <button className="camera-primary-button" type="button" onClick={onCapture}>
            拍照
          </button>
        )}
        {hasCapture && status !== 'success' && (
          <>
            <button className="camera-secondary-button" type="button" onClick={onRetake}>
              重拍
            </button>
            <button
              className="camera-primary-button"
              type="button"
              onClick={onUpload}
              disabled={!displayName.trim() || status === 'uploading'}
            >
              {status === 'uploading' ? '正在上传…' : '确认并上传'}
            </button>
          </>
        )}
        {status === 'success' && (
          <button className="camera-secondary-button" type="button" onClick={onRetake}>
            重新录入
          </button>
        )}
      </div>

      {status === 'success' && (
        <p className="camera-record-id">记录 ID：{sampleId}</p>
      )}
    </section>
  );
}

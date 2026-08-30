import { useEffect, useState } from 'react';
import { Save, Server } from 'lucide-react';

import { configDesktopGateway } from '../../services/config/configDesktopGateway';
import type {
  AppConfig,
  AppConfigField,
  AppConfigFieldResolution,
  AppConfigResolution,
} from '../../shared/appConfig';

interface FieldSpec {
  field: AppConfigField;
  inputId: string;
  label: string;
  placeholder: string;
  hint: string;
  /** 令牌只说配没配，不把值印在界面上。 */
  secret?: boolean;
}

const FIELDS: FieldSpec[] = [
  {
    field: 'serverUrl',
    inputId: 'config-server-url',
    label: 'Server 地址',
    placeholder: 'http://127.0.0.1:8003',
    hint: '摄像头、飞书、番茄钟、告警与机器人推送共用这一个地址。',
  },
  {
    field: 'deviceId',
    inputId: 'config-device-id',
    label: '机器人设备号',
    placeholder: '留空则自动发现',
    hint: '留空时只有恰好一台设备在线才会自动认领，多台不猜。',
  },
  {
    field: 'authToken',
    inputId: 'config-auth-token',
    label: 'Server 访问令牌',
    placeholder: '本机联调通常留空',
    hint: '仅在握手请求头里发送，不会写进任何上屏文本。',
    secret: true,
  },
];

const SOURCE_LABELS = {
  env: '环境变量',
  file: '配置文件',
  default: '默认值',
} as const;

const emptyDraft: AppConfig = { serverUrl: '', deviceId: '', authToken: '' };

const draftOf = (resolution: AppConfigResolution): AppConfig => ({
  // 输入框编辑的永远是「配置文件」那一层：env 覆盖期间也让用户看清
  // 自己存的是什么，摘掉环境变量后立刻生效。
  serverUrl: resolution.serverUrl.fileValue,
  deviceId: resolution.deviceId.fileValue,
  authToken: resolution.authToken.fileValue,
});

const effectiveLabel = (
  field: AppConfigFieldResolution,
  secret: boolean,
): string => {
  if (secret) return field.value ? '已配置' : '未配置';
  return field.value || '（空）';
};

const sourceLabel = (field: AppConfigFieldResolution): string =>
  field.source === 'env'
    ? `${SOURCE_LABELS.env} ${field.envVar ?? ''}`.trim()
    : SOURCE_LABELS[field.source];

interface ServerSettingsPanelProps {
  /** 首次读取与每次保存后回传，供上层点亮链路状态。 */
  onResolved?: (resolution: AppConfigResolution) => void;
}

/**
 * 「连接工作台」的设置面板：读写主进程配置中心。
 *
 * 这里曾经连着永远抛错的 PlaceholderServerGateway——输入框能填、能按保存，
 * 但什么都没发生。现在保存真的落盘，并且逐字段说清生效值来自 env / 文件 / 默认值。
 */
export function ServerSettingsPanel({ onResolved }: ServerSettingsPanelProps) {
  const [resolution, setResolution] = useState<AppConfigResolution | null>(null);
  const [draft, setDraft] = useState<AppConfig>(emptyDraft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [savedAt, setSavedAt] = useState('');

  useEffect(() => {
    let active = true;
    void configDesktopGateway.get()
      .then((next) => {
        if (!active) return;
        setResolution(next);
        setDraft(draftOf(next));
        onResolved?.(next);
      })
      .catch((cause: unknown) => {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : '无法读取配置。');
      });
    return () => { active = false; };
    // 只在挂载时读一次：onResolved 只是回传通道，跟着它重新订阅没有意义
  }, []);

  const save = async (): Promise<void> => {
    setBusy(true);
    setError('');
    try {
      const next = await configDesktopGateway.update({ ...draft });
      setResolution(next);
      setDraft(draftOf(next));
      setSavedAt(new Intl.DateTimeFormat('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).format(new Date()));
      onResolved?.(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '配置保存失败。');
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="connection-panel">
      <div className="panel-heading">
        <div>
          <span className="panel-kicker">CONNECTION</span>
          <h2>连接工作台</h2>
        </div>
        <span className="panel-icon" aria-hidden="true"><Server size={18} /></span>
      </div>

      {error && <p className="config-error" role="alert">{error}</p>}

      <div className="config-fields">
        {FIELDS.map((spec) => {
          const field = resolution?.[spec.field];
          return (
            <div className="config-field" key={spec.field}>
              <label htmlFor={spec.inputId}>{spec.label}</label>
              <input
                id={spec.inputId}
                type={spec.secret ? 'password' : 'text'}
                value={draft[spec.field]}
                placeholder={spec.placeholder}
                disabled={resolution === null || busy}
                onChange={(event) => setDraft((current) => ({
                  ...current,
                  [spec.field]: event.target.value,
                }))}
              />
              <p className="config-effective">
                <span>生效值</span>
                <strong>{field ? effectiveLabel(field, spec.secret ?? false) : '读取中…'}</strong>
                {field && <em>来源 {sourceLabel(field)}</em>}
              </p>
              {field?.source === 'env' && (
                <p className="config-override">
                  环境变量 {field.envVar} 覆盖中，界面修改将在去掉环境变量后生效。
                </p>
              )}
              <small>{spec.hint}</small>
            </div>
          );
        })}
      </div>

      <div className="config-actions">
        <button
          className="config-save"
          type="button"
          disabled={resolution === null || busy}
          onClick={() => void save()}
        >
          <Save size={15} aria-hidden="true" />
          {busy ? '保存中…' : '保存并生效'}
        </button>
        {savedAt && <span className="config-saved">已保存 {savedAt}</span>}
      </div>

      <p className="panel-note">
        保存后立即写入 userData 下的 config.json，各链路在下一次请求时取新值；
        摄像头会断开重连到新地址，无需重启应用。
      </p>
    </aside>
  );
}

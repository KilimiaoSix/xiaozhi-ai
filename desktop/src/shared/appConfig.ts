/**
 * 桌面端配置中心的取值契约：主进程存储、IPC 与设置面板共用这一份。
 *
 * 逐字段优先级固定为 **环境变量 > 配置文件 > 默认值**。
 * env 那一层留给脚本与演示的一次性覆盖，界面改的是配置文件那一层——
 * 所以 env 在场时界面必须明说「改了也不生效」，而不是假装保存成功。
 */
export interface AppConfig {
  /** Server 的 HTTP 根地址，摄像头链路会自行换成 ws/wss。 */
  serverUrl: string;
  /** 机器人设备号；留空时由 deviceDiscovery 在恰好一台在线时自动发现。 */
  deviceId: string;
  /** Server 的 Bearer 令牌，本机联调通常为空。 */
  authToken: string;
}

export type AppConfigField = keyof AppConfig;

export type AppConfigSource = 'env' | 'file' | 'default';

/** 全仓唯一的默认地址常量：各客户端一律经配置中心取值，不再各留一份。 */
export const DEFAULT_APP_CONFIG: AppConfig = {
  serverUrl: 'http://127.0.0.1:8003',
  deviceId: '',
  authToken: '',
};

/**
 * 每个字段认哪些环境变量，数组顺序即优先序。
 *
 * serverUrl 认两个名字是历史包袱：摄像头链路一直读 `XIAOFEI_SERVER_URL`
 * （见 README 的摄像头联调段），飞书/关怀/机器人推送读 `DESKPET_SERVER`。
 * 两者同时存在时以 `DESKPET_SERVER` 为准——它覆盖的链路更多，仓库根的
 * `gongban` 启动器与 `tools/demo-*.sh` 也用它——并打日志说明另一个被忽略，
 * 免得改了没生效的那半天排查。
 */
export const APP_CONFIG_ENV_VARS: Record<AppConfigField, readonly string[]> = {
  serverUrl: ['DESKPET_SERVER', 'XIAOFEI_SERVER_URL'],
  deviceId: ['DESKPET_DEVICE_ID'],
  authToken: ['DESKPET_SERVER_AUTH_TOKEN', 'XIAOFEI_SERVER_AUTH_TOKEN'],
};

export interface AppConfigFieldResolution {
  /** 当前真正生效的值 */
  value: string;
  source: AppConfigSource;
  /** source 为 env 时是哪个变量在覆盖，界面据此指名道姓 */
  envVar?: string;
  /** 配置文件里存着的值：env 覆盖期间界面仍要展示它 */
  fileValue: string;
}

export type AppConfigResolution = {
  [K in AppConfigField]: AppConfigFieldResolution;
};

export interface ResolveAppConfigOptions {
  file?: Partial<AppConfig>;
  env?: Record<string, string | undefined>;
  /** 同一字段有多个环境变量同时在场时报一次，说明谁赢 */
  onWarn?: (message: string) => void;
}

const APP_CONFIG_FIELDS: readonly AppConfigField[] = [
  'serverUrl',
  'deviceId',
  'authToken',
];

/** 空串与纯空白一律当作「没设」：`DESKPET_SERVER=` 不该把地址覆盖成空。 */
const cleaned = (value: unknown): string =>
  typeof value === 'string' ? value.trim() : '';

/**
 * serverUrl 尾斜杠归一：这里是字段解析的单点，env 层与文件层都会经过它。
 *
 * 配置里的 serverUrl 若以 `/` 结尾，pomodoro/incident/deviceDiscovery/
 * wellbeing 四条按 `${baseUrl}${path}` 拼接的链路会拼出 `//xiaozhi/...`
 * 404——只有 feishu/away 两处自己在客户端里局部归一了。归一放在这一处，
 * 下游谁都不用再各自处理；只动 serverUrl，authToken 之类字段原样保留。
 */
const normalizeServerUrl = (field: AppConfigField, value: string): string =>
  field === 'serverUrl' ? value.replace(/\/+$/, '') : value;

const resolveField = (
  field: AppConfigField,
  options: ResolveAppConfigOptions,
): AppConfigFieldResolution => {
  const env = options.env ?? {};
  const present = APP_CONFIG_ENV_VARS[field]
    .map((name) => ({ name, value: normalizeServerUrl(field, cleaned(env[name])) }))
    .filter((candidate) => candidate.value !== '');
  const fileValue = normalizeServerUrl(field, cleaned(options.file?.[field]));

  if (present.length > 1) {
    const [winner, ...ignored] = present;
    options.onWarn?.(
      `环境变量 ${winner.name} 与 ${ignored.map((item) => item.name).join('、')} 同时存在，`
      + `${field} 以 ${winner.name}=${winner.value} 为准，其余被忽略。`,
    );
  }
  if (present.length > 0) {
    return {
      value: present[0].value,
      source: 'env',
      envVar: present[0].name,
      fileValue,
    };
  }
  if (fileValue !== '') {
    return { value: fileValue, source: 'file', fileValue };
  }
  return { value: DEFAULT_APP_CONFIG[field], source: 'default', fileValue };
};

export const resolveAppConfig = (
  options: ResolveAppConfigOptions = {},
): AppConfigResolution => ({
  serverUrl: resolveField('serverUrl', options),
  deviceId: resolveField('deviceId', options),
  authToken: resolveField('authToken', options),
});

/** 把逐字段的解析结果压回一份普通配置，供各链路按次取值。 */
export const configOf = (resolution: AppConfigResolution): AppConfig => ({
  serverUrl: resolution.serverUrl.value,
  deviceId: resolution.deviceId.value,
  authToken: resolution.authToken.value,
});

/** 只留已知字段的字符串值，坏字段直接丢掉而不是让整份配置作废。 */
export const sanitizeAppConfigPatch = (value: unknown): Partial<AppConfig> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const record = value as Record<string, unknown>;
  const patch: Partial<AppConfig> = {};
  for (const field of APP_CONFIG_FIELDS) {
    if (typeof record[field] === 'string') patch[field] = record[field].trim();
  }
  return patch;
};

const SERVER_URL_PROTOCOLS = new Set(['http:', 'https:']);

/**
 * serverUrl 缺协议头（如 "localhost:8003"）时，摄像头客户端的 http→ws
 * 转换会同步抛异常，热切换直接炸掉连接。校验放在 IPC 落盘前的入口，
 * 用户保存时就能看到清楚的中文报错，而不是等摄像头链路死给你看。
 * 空字符串放行——那是「清空该字段，落回默认值」的合法输入。
 */
const validateServerUrl = (value: string): void => {
  if (value === '') return;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(
      `Server 地址格式无效，需包含协议头（例如 http://127.0.0.1:8003），当前值：${value}`,
    );
  }
  if (!SERVER_URL_PROTOCOLS.has(parsed.protocol)) {
    throw new Error(`Server 地址必须以 http:// 或 https:// 开头，当前值：${value}`);
  }
};

/** IPC 入口用：字段或类型不对就报错，而不是悄悄吞掉用户的输入。 */
export const parseAppConfigPatch = (value: unknown): Partial<AppConfig> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('配置更新必须是对象');
  }
  const record = value as Record<string, unknown>;
  const unexpected = Object.keys(record).filter(
    (key) => !(APP_CONFIG_FIELDS as readonly string[]).includes(key),
  );
  if (unexpected.length > 0) {
    throw new Error(`未知配置字段：${unexpected.join('、')}`);
  }
  const patch: Partial<AppConfig> = {};
  for (const field of APP_CONFIG_FIELDS) {
    if (record[field] === undefined) continue;
    if (typeof record[field] !== 'string') {
      throw new Error(`配置字段 ${field} 必须是字符串`);
    }
    patch[field] = (record[field] as string).trim();
  }
  if (patch.serverUrl !== undefined) validateServerUrl(patch.serverUrl);
  return patch;
};

export const sameAppConfig = (left: AppConfig, right: AppConfig): boolean =>
  APP_CONFIG_FIELDS.every((field) => left[field] === right[field]);

import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

import {
  configOf,
  resolveAppConfig,
  sameAppConfig,
  sanitizeAppConfigPatch,
  type AppConfig,
  type AppConfigResolution,
} from '../../shared/appConfig';

export interface AppConfigStoreOptions {
  /** userData 下的 JSON 路径，由 main.ts 用 app.getPath('userData') 拼出。 */
  filePath: string;
  /** 默认读 process.env；测试与预演传自己的表。 */
  env?: Record<string, string | undefined>;
  onWarn?: (message: string) => void;
}

/**
 * 各链路只需要「按次取值 + 变更订阅」这两件事，不需要写权限。
 * 摄像头客户端等消费方依赖这个窄接口，测试里塞个假实现即可。
 */
export interface AppConfigReader {
  get(): AppConfig;
  subscribe(listener: (config: AppConfig) => void): () => void;
}

/**
 * 桌面端唯一的配置事实源。
 *
 * 文件那一层常驻内存（`load()` 之后 `get()` 是同步的），env 那一层每次解析
 * 现读——各链路都在连接/请求时调 `get()`，构造期不缓存地址，所以设置面板
 * 保存完下一次请求就打到新地址，不用重启应用。
 *
 * 写盘沿用 agent-hooks runner 的 `tmp + rename`：半截的 config.json 会让
 * 下次启动整份配置作废，比没保存更糟。
 */
export class AppConfigStore implements AppConfigReader {
  private readonly filePath: string;
  private readonly env: Record<string, string | undefined>;
  private readonly onWarn: (message: string) => void;
  private readonly warned = new Set<string>();
  private readonly listeners = new Set<(config: AppConfig) => void>();
  private fileValues: Partial<AppConfig> = {};
  /**
   * 落盘写队列：串行化而不是给临时文件名加随机后缀。
   *
   * 只加随机后缀治标不治本——两次 rename 仍会乱序落地，后写完的可能反而
   * 先 rename，静默丢掉先提交的那次 patch。串行化才是正解：同一时刻只有
   * 一次 persist() 在跑，且合并基准（this.fileValues）取的是"轮到自己时"
   * 的最新值，天然按提交顺序叠加两次并发 patch，不丢任何一次。
   */
  private writeQueue: Promise<void> = Promise.resolve();

  constructor(options: AppConfigStoreOptions) {
    this.filePath = options.filePath;
    this.env = options.env ?? process.env;
    this.onWarn = options.onWarn ?? ((message) => { console.warn(message); });
  }

  /**
   * 读配置文件。文件缺失、坏 JSON、字段类型不对一律退回默认值继续跑：
   * 配置读不出来就打不开应用，比用默认地址跑着更难查。
   */
  async load(): Promise<void> {
    let text: string;
    try {
      text = await readFile(this.filePath, 'utf8');
    } catch {
      this.fileValues = {};
      return;
    }
    try {
      this.fileValues = sanitizeAppConfigPatch(JSON.parse(text));
    } catch {
      this.fileValues = {};
      this.warn(`配置文件解析失败，暂按默认配置运行：${this.filePath}`);
    }
  }

  get(): AppConfig {
    return configOf(this.resolve());
  }

  /** 每字段的生效值与来源（env / file / default），设置面板直接照着渲染。 */
  resolve(): AppConfigResolution {
    return resolveAppConfig({
      file: this.fileValues,
      env: this.env,
      onWarn: (message) => { this.warn(message); },
    });
  }

  /**
   * 部分更新：只动传进来的字段，其余保持原样。
   *
   * 内存态先于落盘提交是另一个隐患：写盘失败时 fileValues 已经变了，
   * get()/resolve() 会报告一个从未真正落盘的值。这里改成落盘成功后才
   * 提交 this.fileValues——失败时 update() reject，内存/磁盘/UI 三态
   * 保持一致（仍是旧值），不通知订阅者。
   */
  async update(patch: Partial<AppConfig>): Promise<AppConfigResolution> {
    const before = this.get();
    const sanitized = sanitizeAppConfigPatch(patch);
    // 合并基准放进队列回调里读，而不是在这里立刻算好：并发的第二次 update
    // 要等第一次真正提交（this.fileValues 更新）之后再合并，否则两次 patch
    // 都是各自基于旧值算出来的，后写完的会把先提交的那次覆盖掉。
    const run = this.writeQueue.then(async () => {
      const next = { ...this.fileValues, ...sanitized };
      await this.persist(next);
      this.fileValues = next;
    });
    this.writeQueue = run.catch(() => {});
    await run;
    const resolution = this.resolve();
    // 生效值没变就不通知：被 env 遮住的保存不该把摄像头链路踢下线重连。
    if (!sameAppConfig(before, configOf(resolution))) {
      for (const listener of this.listeners) listener(configOf(resolution));
    }
    return resolution;
  }

  subscribe(listener: (config: AppConfig) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** 只管把 `next` 写盘；提交到 this.fileValues 是调用方 update() 的事。 */
  private async persist(next: Partial<AppConfig>): Promise<void> {
    await mkdir(path.dirname(this.filePath), { recursive: true });
    const temporaryPath = `${this.filePath}.${process.pid}.tmp`;
    await writeFile(
      temporaryPath,
      `${JSON.stringify(next, null, 2)}\n`,
      'utf8',
    );
    await rename(temporaryPath, this.filePath);
  }

  /** 同一条警告只报一次：resolve() 每次请求都跑，否则日志会被刷屏。 */
  private warn(message: string): void {
    if (this.warned.has(message)) return;
    this.warned.add(message);
    this.onWarn(message);
  }
}

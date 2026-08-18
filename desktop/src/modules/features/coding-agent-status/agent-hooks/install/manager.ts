import { execFile } from 'node:child_process';
import {
  access,
  copyFile,
  mkdir,
  readFile,
  rename,
  rm,
  writeFile,
} from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';

import type { AgentSource } from '../contracts';
import {
  createOwnedHookCommand,
  hasOwnedHooks,
  mergeOwnedHooks,
  ownedHooksMatchCommand,
  unmergeOwnedHooks,
} from './jsonHookConfig';
import { SOURCE_DEFINITIONS } from './sourceDefinitions';
import type { AgentHookDetection, AgentHookInstallResult } from './types';

const execFileAsync = promisify(execFile);
const SOURCES: AgentSource[] = ['codex', 'claude-code', 'workbuddy'];

interface AgentHookManagerOptions {
  homeDir: string;
  launcherPath: string;
  spoolPath: string;
  codexTrustMarkerPath?: string;
  now?: () => Date;
  resolveExecutable?: (source: AgentSource) => Promise<string | undefined>;
  workBuddyAppPaths?: string[];
  ensureRunner?: () => Promise<void>;
}

const exists = async (target: string): Promise<boolean> => {
  try {
    await access(target);
    return true;
  } catch {
    return false;
  }
};

const parseConfig = (content: string): Record<string, unknown> => {
  const value: unknown = JSON.parse(content);
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('配置 JSON 根节点必须是对象');
  }
  return value as Record<string, unknown>;
};

export class AgentHookManager {
  private readonly now: () => Date;
  private readonly resolveExecutable: (source: AgentSource) => Promise<string | undefined>;
  private readonly workBuddyAppPaths: string[];
  private readonly ensureRunner: () => Promise<void>;

  constructor(private readonly options: AgentHookManagerOptions) {
    this.now = options.now ?? (() => new Date());
    this.resolveExecutable = options.resolveExecutable ?? ((source) =>
      this.resolveFromLoginShell(source));
    this.workBuddyAppPaths = options.workBuddyAppPaths ?? [
      '/Applications/WorkBuddy.app',
      path.join(options.homeDir, 'Applications/WorkBuddy.app'),
    ];
    this.ensureRunner = options.ensureRunner ?? (async () => undefined);
  }

  async prepare(): Promise<void> {
    await this.ensureRunner();
  }

  async detect(): Promise<AgentHookDetection[]> {
    return Promise.all(SOURCES.map((source) => this.detectSource(source)));
  }

  async installAll(): Promise<AgentHookInstallResult[]> {
    const results: AgentHookInstallResult[] = [];
    for (const source of SOURCES) results.push(await this.install(source));
    return results;
  }

  async install(source: AgentSource): Promise<AgentHookInstallResult> {
    const detection = await this.detectSource(source);
    if (!detection.available) {
      return {
        source,
        ok: false,
        installed: false,
        configPath: detection.configPath,
        message: `未发现 ${this.displayName(source)}`,
      };
    }

    try {
      const hadConfig = await exists(detection.configPath);
      const content = hadConfig ? await readFile(detection.configPath, 'utf8') : '{}';
      const config = parseConfig(content);
      await this.prepare();
      const command = this.ownedCommand(source);
      if (ownedHooksMatchCommand(config, SOURCE_DEFINITIONS[source], command)) {
        const requiresTrustReview = source === 'codex'
          && await this.codexTrustReviewRequired();
        return {
          source,
          ok: true,
          installed: true,
          ...(source === 'codex' ? { requiresTrustReview } : {}),
          configPath: detection.configPath,
          message: source === 'codex'
            ? (requiresTrustReview
                ? 'Hook 已配置，请在 Codex 中运行 /hooks 完成授权'
                : '监控 Hook 已生效')
            : '监控 Hook 已安装',
        };
      }

      const refreshing = hasOwnedHooks(config);
      const merged = mergeOwnedHooks(config, SOURCE_DEFINITIONS[source], command);
      const backupPath = hadConfig
        ? await this.backup(detection.configPath)
        : undefined;
      await this.atomicWrite(detection.configPath, merged);
      if (source === 'codex') await this.markCodexTrustReviewRequired();

      return {
        source,
        ok: true,
        installed: true,
        ...(source === 'codex' ? { requiresTrustReview: true } : {}),
        configPath: detection.configPath,
        ...(backupPath ? { backupPath } : {}),
        message: source === 'codex'
          ? 'Hook 已配置，请在 Codex 中运行 /hooks 完成授权'
          : (refreshing ? '监控 Hook 已刷新' : '监控 Hook 安装成功'),
      };
    } catch (error) {
      return {
        source,
        ok: false,
        installed: false,
        configPath: detection.configPath,
        message: error instanceof SyntaxError
          ? `配置 JSON 无法解析：${error.message}`
          : (error instanceof Error ? error.message : 'Hook 安装失败'),
      };
    }
  }

  async uninstall(source: AgentSource): Promise<AgentHookInstallResult> {
    const configPath = await this.configPathFor(source);
    if (!await exists(configPath)) {
      await this.markActive(source);
      return {
        source,
        ok: true,
        installed: false,
        configPath,
        message: '没有需要卸载的监控 Hook',
      };
    }

    try {
      const config = parseConfig(await readFile(configPath, 'utf8'));
      if (!hasOwnedHooks(config)) {
        await this.markActive(source);
        return {
          source,
          ok: true,
          installed: false,
          configPath,
          message: '没有需要卸载的监控 Hook',
        };
      }
      const backupPath = await this.backup(configPath);
      await this.atomicWrite(configPath, unmergeOwnedHooks(config));
      await this.markActive(source);
      return {
        source,
        ok: true,
        installed: false,
        configPath,
        backupPath,
        message: '监控 Hook 已卸载',
      };
    } catch (error) {
      return {
        source,
        ok: false,
        installed: true,
        configPath,
        message: error instanceof SyntaxError
          ? `配置 JSON 无法解析：${error.message}`
          : (error instanceof Error ? error.message : 'Hook 卸载失败'),
      };
    }
  }

  async markActive(source: AgentSource): Promise<void> {
    if (source !== 'codex' || !this.options.codexTrustMarkerPath) return;
    await rm(this.options.codexTrustMarkerPath, { force: true });
  }

  private async detectSource(source: AgentSource): Promise<AgentHookDetection> {
    const configPath = await this.configPathFor(source);
    const executablePath = await this.resolveExecutable(source);
    const configExists = await exists(configPath);
    const appExists = source === 'workbuddy'
      && (await Promise.all(this.workBuddyAppPaths.map(exists))).some(Boolean);
    const available = Boolean(executablePath) || configExists || appExists;

    let installed = false;
    let requiresTrustReview = false;
    let detail = available ? '已发现，尚未启用监控' : '未发现';
    if (configExists) {
      try {
        const config = parseConfig(await readFile(configPath, 'utf8'));
        installed = ownedHooksMatchCommand(
          config,
          SOURCE_DEFINITIONS[source],
          this.ownedCommand(source),
        );
        if (installed && source === 'codex') {
          requiresTrustReview = await this.codexTrustReviewRequired();
        }
        if (installed) {
          detail = source === 'codex'
            ? (requiresTrustReview
                ? 'Hook 已配置，请在 Codex 中运行 /hooks 完成授权'
                : '监控 Hook 已生效')
            : '监控 Hook 已安装';
        }
        else if (hasOwnedHooks(config)) detail = '监控 Hook 路径已失效，请重新接入';
      } catch (error) {
        detail = `配置 JSON 无法解析：${error instanceof Error ? error.message : '未知错误'}`;
      }
    }

    return {
      source,
      available,
      installed,
      ...(source === 'codex' ? { requiresTrustReview } : {}),
      ...(executablePath ? { executablePath } : {}),
      configPath,
      message: detail,
    };
  }

  private async configPathFor(source: AgentSource): Promise<string> {
    if (source === 'codex') return path.join(this.options.homeDir, '.codex/hooks.json');
    if (source === 'claude-code') return path.join(this.options.homeDir, '.claude/settings.json');

    const workbuddy = path.join(this.options.homeDir, '.workbuddy/settings.json');
    const codebuddy = path.join(this.options.homeDir, '.codebuddy/settings.json');
    if (await exists(workbuddy)) return workbuddy;
    if (await exists(codebuddy)) return codebuddy;
    return workbuddy;
  }

  private async resolveFromLoginShell(source: AgentSource): Promise<string | undefined> {
    for (const command of SOURCE_DEFINITIONS[source].commandNames) {
      try {
        const { stdout } = await execFileAsync(
          '/bin/zsh',
          ['-lc', `command -v ${command}`],
          { timeout: 2_000 },
        );
        const value = stdout.trim();
        if (value) return value;
      } catch {
        // Try the next known command name.
      }
    }
    return undefined;
  }

  private async backup(configPath: string): Promise<string> {
    const timestamp = this.now().toISOString().replace(/[.:]/g, '-');
    const backupPath = `${configPath}.${timestamp}.launchcrush.bak`;
    await copyFile(configPath, backupPath);
    return backupPath;
  }

  private async atomicWrite(
    configPath: string,
    config: Record<string, unknown>,
  ): Promise<void> {
    await mkdir(path.dirname(configPath), { recursive: true });
    const temporaryPath = `${configPath}.${process.pid}.${Date.now()}.tmp`;
    await writeFile(temporaryPath, `${JSON.stringify(config, null, 2)}\n`, 'utf8');
    await rename(temporaryPath, configPath);
  }

  private async codexTrustReviewRequired(): Promise<boolean> {
    return this.options.codexTrustMarkerPath
      ? exists(this.options.codexTrustMarkerPath)
      : false;
  }

  private async markCodexTrustReviewRequired(): Promise<void> {
    const markerPath = this.options.codexTrustMarkerPath;
    if (!markerPath) return;
    await mkdir(path.dirname(markerPath), { recursive: true });
    await writeFile(markerPath, `${this.now().toISOString()}\n`, 'utf8');
  }

  private displayName(source: AgentSource): string {
    if (source === 'claude-code') return 'Claude Code';
    if (source === 'workbuddy') return 'WorkBuddy';
    return 'Codex';
  }

  private ownedCommand(source: AgentSource): string {
    return createOwnedHookCommand({
      launcherPath: this.options.launcherPath,
      spoolPath: this.options.spoolPath,
      source,
    });
  }
}

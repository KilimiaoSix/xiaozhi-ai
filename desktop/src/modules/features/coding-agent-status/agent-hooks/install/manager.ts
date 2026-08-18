import { execFile } from 'node:child_process';
import {
  access,
  copyFile,
  mkdir,
  readFile,
  rename,
  writeFile,
} from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';

import type { AgentSource } from '../contracts';
import {
  createOwnedHookCommand,
  hasOwnedHooks,
  mergeOwnedHooks,
  unmergeOwnedHooks,
} from './jsonHookConfig';
import { SOURCE_DEFINITIONS } from './sourceDefinitions';
import type { AgentHookDetection, AgentHookInstallResult } from './types';

const execFileAsync = promisify(execFile);
const SOURCES: AgentSource[] = ['codex', 'claude-code', 'workbuddy'];

interface AgentHookManagerOptions {
  homeDir: string;
  electronPath: string;
  runnerPath: string;
  spoolPath: string;
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
      if (hasOwnedHooks(config)) {
        return {
          source,
          ok: true,
          installed: true,
          configPath: detection.configPath,
          message: '监控 Hook 已安装',
        };
      }

      await this.ensureRunner();
      const command = createOwnedHookCommand({
        electronPath: this.options.electronPath,
        runnerPath: this.options.runnerPath,
        spoolPath: this.options.spoolPath,
        source,
      });
      const merged = mergeOwnedHooks(config, SOURCE_DEFINITIONS[source], command);
      const backupPath = hadConfig
        ? await this.backup(detection.configPath)
        : undefined;
      await this.atomicWrite(detection.configPath, merged);

      return {
        source,
        ok: true,
        installed: true,
        configPath: detection.configPath,
        ...(backupPath ? { backupPath } : {}),
        message: '监控 Hook 安装成功',
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

  private async detectSource(source: AgentSource): Promise<AgentHookDetection> {
    const configPath = await this.configPathFor(source);
    const executablePath = await this.resolveExecutable(source);
    const configExists = await exists(configPath);
    const appExists = source === 'workbuddy'
      && (await Promise.all(this.workBuddyAppPaths.map(exists))).some(Boolean);
    const available = Boolean(executablePath) || configExists || appExists;

    let installed = false;
    let detail = available ? '已发现，尚未启用监控' : '未发现';
    if (configExists) {
      try {
        installed = hasOwnedHooks(parseConfig(await readFile(configPath, 'utf8')));
        if (installed) detail = '监控 Hook 已安装';
      } catch (error) {
        detail = `配置 JSON 无法解析：${error instanceof Error ? error.message : '未知错误'}`;
      }
    }

    return {
      source,
      available,
      installed,
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

  private displayName(source: AgentSource): string {
    if (source === 'claude-code') return 'Claude Code';
    if (source === 'workbuddy') return 'WorkBuddy';
    return 'Codex';
  }
}


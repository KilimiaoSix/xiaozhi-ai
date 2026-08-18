import { execFile } from 'node:child_process';
import { homedir } from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

import type {
  FeishuBriefingSnapshot,
  FeishuCliStatus,
  FeishuMeeting,
  FeishuTask,
} from './contracts';

const execFileAsync = promisify(execFile);
const DEFAULT_TIMEOUT_MS = 20_000;
const REQUIRED_SCOPES = ['task:task:read', 'calendar:calendar:readonly'];

type JsonRecord = Record<string, unknown>;

export interface LarkCliCommandResult {
  stdout: string;
  stderr: string;
}

export interface LarkCliCommandRunner {
  run(args: string[]): Promise<LarkCliCommandResult>;
}

export class ExecFileLarkCliCommandRunner implements LarkCliCommandRunner {
  private command: string | undefined;

  async run(args: string[]): Promise<LarkCliCommandResult> {
    const candidates = this.command
      ? [this.command]
      : [
          'lark-cli',
          path.join(homedir(), '.local', 'bin', 'lark-cli'),
          '/opt/homebrew/bin/lark-cli',
          '/usr/local/bin/lark-cli',
        ];

    for (const candidate of candidates) {
      try {
        const result = await this.execute(candidate, args);
        this.command = candidate;
        return result;
      } catch (error) {
        const commandError = error as NodeJS.ErrnoException;
        if (commandError.code === 'ENOENT') continue;
        throw error;
      }
    }
    throw new LarkCliUnavailableError('未找到 lark-cli，请先安装飞书 CLI。');
  }

  private async execute(command: string, args: string[]): Promise<LarkCliCommandResult> {
    try {
      const result = await execFileAsync(command, args, {
        encoding: 'utf8',
        env: {
          ...process.env,
          LARKSUITE_CLI_NO_UPDATE_NOTIFIER: '1',
          LARKSUITE_CLI_NO_SKILLS_NOTIFIER: '1',
        },
        maxBuffer: 4 * 1024 * 1024,
        timeout: DEFAULT_TIMEOUT_MS,
      });
      return { stdout: result.stdout, stderr: result.stderr };
    } catch (error) {
      const commandError = error as NodeJS.ErrnoException & {
        stdout?: string;
        stderr?: string;
      };
      if (commandError.code === 'ENOENT') throw commandError;
      if (commandError.stdout || commandError.stderr) {
        return {
          stdout: commandError.stdout ?? '',
          stderr: commandError.stderr ?? '',
        };
      }
      throw error;
    }
  }
}

class LarkCliUnavailableError extends Error {}

class LarkCliResponseError extends Error {
  constructor(
    message: string,
    readonly missingScopes: string[] = [],
    readonly subtype?: string,
  ) {
    super(message);
  }
}

const isRecord = (value: unknown): value is JsonRecord =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const stringValue = (...values: unknown[]): string | undefined => {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return undefined;
};

const stringArray = (value: unknown): string[] => {
  if (Array.isArray(value)) {
    return value.filter((item): item is string => typeof item === 'string');
  }
  if (typeof value === 'string') return value.split(/\s+/).filter(Boolean);
  return [];
};

const parseJson = (result: LarkCliCommandResult): JsonRecord => {
  const candidates = [result.stdout, result.stderr].map((value) => value.trim()).filter(Boolean);
  for (const candidate of candidates) {
    try {
      const parsed: unknown = JSON.parse(candidate);
      if (isRecord(parsed)) return parsed;
    } catch {
      const start = candidate.indexOf('{');
      const end = candidate.lastIndexOf('}');
      if (start >= 0 && end > start) {
        try {
          const parsed: unknown = JSON.parse(candidate.slice(start, end + 1));
          if (isRecord(parsed)) return parsed;
        } catch {
          // Continue to the next output stream.
        }
      }
    }
  }
  throw new LarkCliResponseError('飞书 CLI 返回了无法识别的结果。');
};

const requireSuccessData = (response: JsonRecord): unknown => {
  if (response.ok === true) return response.data;
  const error = isRecord(response.error) ? response.error : {};
  throw new LarkCliResponseError(
    stringValue(error.message, error.hint) ?? '飞书 CLI 请求失败。',
    stringArray(error.missing_scopes ?? error.permission_violations),
    stringValue(error.subtype, error.type),
  );
};

const itemsFromData = (data: unknown): JsonRecord[] => {
  if (Array.isArray(data)) return data.filter(isRecord);
  if (!isRecord(data)) return [];
  const candidate = data.items ?? data.events ?? data.tasks ?? data.data;
  if (!Array.isArray(candidate)) return [];
  return candidate.filter(isRecord);
};

const normalizedTime = (value: unknown): { value?: string; allDay: boolean } => {
  let raw = value;
  let allDay = false;
  if (isRecord(value)) {
    raw = value.timestamp ?? value.date_time ?? value.datetime ?? value.date ?? value.time;
    allDay = value.is_all_day === true || value.all_day === true || value.date !== undefined;
  }
  if (typeof raw === 'number') raw = String(raw);
  if (typeof raw !== 'string' || !raw.trim()) return { allDay };

  const text = raw.trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return { value: text, allDay: true };
  if (/^\d{10,13}$/.test(text)) {
    const numeric = Number(text);
    const milliseconds = text.length <= 10 ? numeric * 1000 : numeric;
    const date = new Date(milliseconds);
    if (!Number.isNaN(date.getTime())) return { value: date.toISOString(), allDay };
  }
  const date = new Date(text);
  return Number.isNaN(date.getTime())
    ? { value: text, allDay }
    : { value: date.toISOString(), allDay };
};

const firstProjectName = (task: JsonRecord): string | undefined => {
  const direct = stringValue(task.tasklist_name, task.project_name);
  if (direct) return direct;
  const tasklists = task.tasklists ?? task.task_list;
  if (Array.isArray(tasklists)) {
    const first = tasklists.find(isRecord);
    return first ? stringValue(first.name, first.title) : undefined;
  }
  if (isRecord(tasklists)) return stringValue(tasklists.name, tasklists.title);
  return undefined;
};

export const parseMeetings = (data: unknown): FeishuMeeting[] =>
  itemsFromData(data)
    .map((event, index) => {
      const start = normalizedTime(event.start ?? event.start_time);
      const end = normalizedTime(event.end ?? event.end_time);
      return {
        id: stringValue(event.event_id, event.id) ?? `meeting-${index}`,
        title: stringValue(event.summary, event.title, event.name) ?? '未命名日程',
        start: start.value ?? '',
        end: end.value,
        allDay: start.allDay,
        url: stringValue(event.url, event.app_link, event.event_url),
      };
    })
    .filter((event) => event.start)
    .sort((left, right) => left.start.localeCompare(right.start));

export const parseTasks = (data: unknown): FeishuTask[] =>
  itemsFromData(data).map((task, index) => {
    const due = normalizedTime(task.due ?? task.due_time ?? task.deadline);
    return {
      id: stringValue(task.guid, task.task_id, task.id) ?? `task-${index}`,
      title: stringValue(task.summary, task.title, task.name) ?? '未命名任务',
      due: due.value,
      allDay: due.allDay,
      url: stringValue(task.url, task.app_link),
      projectName: firstProjectName(task),
    };
  });

const localDate = (date: Date): string => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
};

const friendlyWarning = (label: string, error: unknown): string => {
  if (error instanceof LarkCliResponseError && error.missingScopes.length) {
    return `${label}缺少飞书权限：${error.missingScopes.join('、')}`;
  }
  return `${label}读取失败：${error instanceof Error ? error.message : '未知错误'}`;
};

export class LarkCliClient {
  constructor(private readonly runner: LarkCliCommandRunner = new ExecFileLarkCliCommandRunner()) {}

  async getStatus(): Promise<FeishuCliStatus> {
    let cliVersion: string | undefined;
    try {
      const versionResult = await this.runner.run(['--version']);
      cliVersion = stringValue(versionResult.stdout)?.replace(/^lark-cli version\s+/i, '');
      const response = parseJson(await this.runner.run(['auth', 'status', '--json', '--verify']));
      if (response.ok === false) {
        const error = isRecord(response.error) ? response.error : {};
        const message = stringValue(error.message, error.hint) ?? '飞书 CLI 尚未完成配置。';
        const notConfigured = /not initialized|config init|keychain/i.test(message);
        return {
          state: notConfigured ? 'not_configured' : 'needs_auth',
          message,
          cliVersion,
          scopes: [],
          missingScopes: REQUIRED_SCOPES,
        };
      }

      const identities = isRecord(response.identities) ? response.identities : {};
      const user = isRecord(identities.user) ? identities.user : {};
      const scopes = stringArray(user.scope);
      const available = user.available === true;
      const verified = user.verified === true || response.verified === true;
      const missingScopes = REQUIRED_SCOPES.filter((scope) => !scopes.includes(scope));
      return {
        state: available && verified ? 'ready' : 'needs_auth',
        message: stringValue(user.message, response.note) ??
          (available && verified ? '飞书用户身份已连接。' : '需要登录飞书用户身份。'),
        cliVersion,
        userName: stringValue(user.userName),
        openId: stringValue(user.openId),
        scopes,
        missingScopes,
      };
    } catch (error) {
      if (error instanceof LarkCliUnavailableError) {
        return {
          state: 'cli_missing',
          message: error.message,
          scopes: [],
          missingScopes: REQUIRED_SCOPES,
        };
      }
      return {
        state: 'error',
        message: error instanceof Error ? error.message : '无法检查飞书 CLI。',
        cliVersion,
        scopes: [],
        missingScopes: REQUIRED_SCOPES,
      };
    }
  }

  async getBriefing(now = new Date()): Promise<FeishuBriefingSnapshot> {
    const status = await this.getStatus();
    if (status.state !== 'ready') {
      throw new Error(status.message);
    }

    const date = localDate(now);
    const [meetingResult, taskResult] = await Promise.allSettled([
      this.runner.run([
        'calendar', '+agenda', '--as', 'user', '--start', date, '--end', date, '--json',
      ]),
      this.runner.run([
        'task', '+get-my-tasks', '--as', 'user', '--complete=false', '--page-limit', '3', '--json',
      ]),
    ]);

    const warnings: string[] = [];
    let meetings: FeishuMeeting[] = [];
    let tasks: FeishuTask[] = [];

    try {
      if (meetingResult.status === 'rejected') throw meetingResult.reason;
      meetings = parseMeetings(requireSuccessData(parseJson(meetingResult.value)));
    } catch (error) {
      warnings.push(friendlyWarning('今日日程', error));
    }

    try {
      if (taskResult.status === 'rejected') throw taskResult.reason;
      tasks = parseTasks(requireSuccessData(parseJson(taskResult.value)));
    } catch (error) {
      warnings.push(friendlyWarning('未完成任务', error));
    }

    if (warnings.length === 2) {
      throw new Error(warnings.join('；'));
    }

    return {
      status,
      date,
      meetings,
      tasks,
      warnings,
      fetchedAt: now.toISOString(),
    };
  }
}

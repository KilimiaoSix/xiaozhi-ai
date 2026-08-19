import { mkdir, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { CodexUiApprovalMonitor } from '../modules/features/coding-agent-status/agent-hooks/codexUiApprovalMonitor';
import { AgentHookManager } from '../modules/features/coding-agent-status/agent-hooks/install/manager';
import { AgentHooksRuntime } from '../modules/features/coding-agent-status/agent-hooks/runtime';
import { EventSpool } from '../modules/features/coding-agent-status/agent-hooks/spool/eventSpool';
import { HOOK_RUNNER_SOURCE } from '../modules/features/coding-agent-status/agent-hooks/spool/hookRunnerSource';
import { AgentTaskTracker } from '../modules/features/coding-agent-status/agent-hooks/taskTracker';
import { createCodexUiApprovalProbe } from './codexUiApprovalProbe';
import { AgentRobotNotifier, RobotEventPushClient } from './agentRobot/agentRobotNotifier';
import { DiscoveringRobotNotifier } from './agentRobot/deviceDiscovery';

interface CreateAgentHooksRuntimeOptions {
  homeDir: string;
  userDataPath: string;
  electronPath: string;
  platform?: NodeJS.Platform;
  isAccessibilityTrusted?: () => boolean;
  robotDeviceId?: string;
  robotServerUrl?: string;
}

const shellQuote = (value: string): string => `'${value.replaceAll("'", `'"'"'`)}'`;

const writeHookRunner = async (
  launcherPath: string,
  runnerPath: string,
  electronPath: string,
): Promise<void> => {
  await mkdir(path.dirname(launcherPath), { recursive: true });
  const temporaryPath = `${runnerPath}.${process.pid}.tmp`;
  await writeFile(temporaryPath, HOOK_RUNNER_SOURCE, { encoding: 'utf8', mode: 0o700 });
  await rename(temporaryPath, runnerPath);

  const launcherTemporaryPath = `${launcherPath}.${process.pid}.tmp`;
  const launcherSource = [
    '#!/bin/sh',
    'export ELECTRON_RUN_AS_NODE=1',
    `exec ${shellQuote(electronPath)} ${shellQuote(runnerPath)} "$@"`,
    '',
  ].join('\n');
  await writeFile(launcherTemporaryPath, launcherSource, { encoding: 'utf8', mode: 0o700 });
  await rename(launcherTemporaryPath, launcherPath);
};

export const createAgentHooksRuntime = (
  options: CreateAgentHooksRuntimeOptions,
): AgentHooksRuntime => {
  const rootPath = path.join(options.userDataPath, 'agent-hooks');
  const launcherPath = path.join(rootPath, 'launchcrush-hook');
  const runnerPath = path.join(rootPath, 'launchcrush-hook.cjs');
  const manager = new AgentHookManager({
    homeDir: options.homeDir,
    launcherPath,
    spoolPath: rootPath,
    codexTrustMarkerPath: path.join(rootPath, 'state/codex-trust-required'),
    ensureRunner: () => writeHookRunner(launcherPath, runnerPath, options.electronPath),
  });
  const platform = options.platform ?? process.platform;
  const attentionMonitor = platform === 'darwin'
    ? new CodexUiApprovalMonitor({
        probe: createCodexUiApprovalProbe({
          platform,
          isAccessibilityTrusted: options.isAccessibilityTrusted,
          onError: (error) => {
            console.warn('Codex 等待批准状态检查失败', error);
          },
        }),
      })
    : undefined;

  // 机器人反馈：把 tracker 产出的意图推给 Server 的 /xiaozhi/event/push。
  // 地址与设备沿用 tools/cc-approval-hook.sh 已有的环境变量约定；
  // 未配置 DESKPET_DEVICE_ID 时退而向 Server 查在线设备表，恰好一台在线才启用
  // （多台不猜、查不到保持静默），没有机器人的开发机上监控照常工作。
  const deviceId = options.robotDeviceId ?? process.env.DESKPET_DEVICE_ID ?? '';
  const serverUrl = options.robotServerUrl
    ?? process.env.DESKPET_SERVER
    ?? 'http://127.0.0.1:8003';
  const onRobotError = (error: unknown) => {
    console.warn('机器人意图处理失败', error);
  };
  const robotNotifier = deviceId
    ? new AgentRobotNotifier(
        new RobotEventPushClient(fetch, serverUrl, (error) => {
          console.warn('推送工作事件到机器人失败', error);
        }),
        deviceId,
        { onError: onRobotError },
      )
    : new DiscoveringRobotNotifier({
        serverUrl,
        onError: onRobotError,
      });

  return new AgentHooksRuntime({
    manager,
    spool: new EventSpool({ rootPath }),
    tracker: new AgentTaskTracker(),
    statePath: path.join(rootPath, 'state/tasks.json'),
    ...(attentionMonitor ? { attentionMonitor } : {}),
    ...(robotNotifier ? { robotNotifier } : {}),
  });
};

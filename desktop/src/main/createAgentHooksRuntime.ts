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
import { ConfiguredRobotNotifier } from './agentRobot/configuredRobotNotifier';
import { DiscoveringRobotNotifier } from './agentRobot/deviceDiscovery';
import type { AppConfigReader } from './config/appConfigStore';

interface CreateAgentHooksRuntimeOptions {
  homeDir: string;
  userDataPath: string;
  electronPath: string;
  /** 机器人链路的地址与设备号唯一来源。 */
  config: AppConfigReader;
  platform?: NodeJS.Platform;
  isAccessibilityTrusted?: () => boolean;
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
  // 地址与设备号一律来自配置中心（env > 配置文件 > 默认值），按条事件现取；
  // 没有设备号时退而向 Server 查在线设备表，恰好一台在线才启用
  // （多台不猜、查不到保持静默），没有机器人的开发机上监控照常工作。
  const resolveServerUrl = () => options.config.get().serverUrl;
  const onRobotError = (error: unknown) => {
    console.warn('机器人意图处理失败', error);
  };
  let discovering: DiscoveringRobotNotifier | undefined;
  const createDiscovering = (): DiscoveringRobotNotifier => {
    discovering ??= new DiscoveringRobotNotifier({
      resolveServerUrl,
      onError: onRobotError,
    });
    return discovering;
  };
  // 启动时就没有设备号的话立刻开始轮询：等第一条事件到了才开始找设备，
  // 那条事件必然被丢掉（发现成功前的意图不补播）。
  if (!options.config.get().deviceId.trim()) createDiscovering();
  const robotNotifier = new ConfiguredRobotNotifier({
    resolveDeviceId: () => options.config.get().deviceId,
    createDirect: (deviceId) => new AgentRobotNotifier(
      new RobotEventPushClient(fetch, resolveServerUrl, (error) => {
        console.warn('推送工作事件到机器人失败', error);
      }),
      deviceId,
      { onError: onRobotError },
    ),
    createDiscovering,
  });

  return new AgentHooksRuntime({
    manager,
    spool: new EventSpool({ rootPath }),
    tracker: new AgentTaskTracker(),
    statePath: path.join(rootPath, 'state/tasks.json'),
    ...(attentionMonitor ? { attentionMonitor } : {}),
    robotNotifier,
  });
};

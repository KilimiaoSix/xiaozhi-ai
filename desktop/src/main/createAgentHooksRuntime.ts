import { mkdir, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { AgentHookManager } from '../modules/features/coding-agent-status/agent-hooks/install/manager';
import { AgentHooksRuntime } from '../modules/features/coding-agent-status/agent-hooks/runtime';
import { EventSpool } from '../modules/features/coding-agent-status/agent-hooks/spool/eventSpool';
import { HOOK_RUNNER_SOURCE } from '../modules/features/coding-agent-status/agent-hooks/spool/hookRunnerSource';
import { AgentTaskTracker } from '../modules/features/coding-agent-status/agent-hooks/taskTracker';

interface CreateAgentHooksRuntimeOptions {
  homeDir: string;
  userDataPath: string;
  electronPath: string;
}

const writeHookRunner = async (runnerPath: string): Promise<void> => {
  await mkdir(path.dirname(runnerPath), { recursive: true });
  const temporaryPath = `${runnerPath}.${process.pid}.tmp`;
  await writeFile(temporaryPath, HOOK_RUNNER_SOURCE, { encoding: 'utf8', mode: 0o700 });
  await rename(temporaryPath, runnerPath);
};

export const createAgentHooksRuntime = (
  options: CreateAgentHooksRuntimeOptions,
): AgentHooksRuntime => {
  const rootPath = path.join(options.userDataPath, 'agent-hooks');
  const runnerPath = path.join(rootPath, 'launchcrush-hook.cjs');
  const manager = new AgentHookManager({
    homeDir: options.homeDir,
    electronPath: options.electronPath,
    runnerPath,
    spoolPath: rootPath,
    ensureRunner: () => writeHookRunner(runnerPath),
  });

  return new AgentHooksRuntime({
    manager,
    spool: new EventSpool({ rootPath }),
    tracker: new AgentTaskTracker(),
    statePath: path.join(rootPath, 'state/tasks.json'),
  });
};


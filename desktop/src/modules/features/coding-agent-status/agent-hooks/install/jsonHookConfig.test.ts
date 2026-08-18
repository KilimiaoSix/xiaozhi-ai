import { describe, expect, it } from 'vitest';

import { SOURCE_DEFINITIONS } from './sourceDefinitions';
import {
  createOwnedHookCommand,
  mergeOwnedHooks,
  unmergeOwnedHooks,
} from './jsonHookConfig';

describe('Hook JSON 配置合并', () => {
  const ownedCommand = createOwnedHookCommand({
    electronPath: '/Applications/工伴.app/Contents/MacOS/工伴',
    runnerPath: '/Users/demo/Library/Application Support/工伴/hooks/launchcrush-hook.cjs',
    spoolPath: '/Users/demo/Library/Application Support/工伴/agent-hooks',
    source: 'codex',
  });

  it('保留未知字段和用户已有 Hook', () => {
    const existing = {
      theme: 'dark',
      hooks: {
        Stop: [{ hooks: [{ type: 'command', command: '/user/notify.sh' }] }],
      },
    };

    const installed = mergeOwnedHooks(
      existing,
      SOURCE_DEFINITIONS.codex,
      ownedCommand,
    );

    expect(installed).toMatchObject({ theme: 'dark' });
    expect(JSON.stringify(installed)).toContain('/user/notify.sh');
    expect(JSON.stringify(installed)).toContain('launchcrush-agent-hook');
  });

  it('重复合并不产生重复 owned handler', () => {
    const once = mergeOwnedHooks({}, SOURCE_DEFINITIONS.codex, ownedCommand);
    const twice = mergeOwnedHooks(once, SOURCE_DEFINITIONS.codex, ownedCommand);

    expect(twice).toEqual(once);
    expect(JSON.stringify(twice).match(/launchcrush-agent-hook/g)).toHaveLength(
      SOURCE_DEFINITIONS.codex.events.length,
    );
  });

  it('只从配置中移除 owned handler', () => {
    const existing = {
      hooks: {
        Stop: [{
          hooks: [
            { type: 'command', command: '/user/notify.sh' },
            { type: 'command', command: ownedCommand },
          ],
        }],
        SessionStart: [{
          matcher: 'startup',
          hooks: [{ type: 'command', command: ownedCommand }],
        }],
      },
    };

    expect(unmergeOwnedHooks(existing)).toEqual({
      hooks: {
        Stop: [{ hooks: [{ type: 'command', command: '/user/notify.sh' }] }],
      },
    });
  });

  it('正确引用命令中的空格和单引号', () => {
    const command = createOwnedHookCommand({
      electronPath: "/Applications/Worker's Desk.app/Contents/MacOS/Desk",
      runnerPath: '/tmp/agent hook.cjs',
      spoolPath: '/tmp/agent spool',
      source: 'claude-code',
    });

    expect(command).toContain("'/Applications/Worker'\"'\"'s Desk.app/Contents/MacOS/Desk'");
    expect(command).toContain("'/tmp/agent hook.cjs'");
    expect(command).toContain('--source claude-code');
    expect(command).toContain('--owner launchcrush-agent-hook');
  });
});

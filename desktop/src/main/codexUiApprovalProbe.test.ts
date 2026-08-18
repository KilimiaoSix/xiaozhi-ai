import { describe, expect, it, vi } from 'vitest';

import { createCodexUiApprovalProbe } from './codexUiApprovalProbe';

describe('createCodexUiApprovalProbe', () => {
  it('在 macOS 上把只读辅助功能脚本结果转换为审批状态', async () => {
    const runScript = vi.fn().mockResolvedValue('true\n');
    const probe = createCodexUiApprovalProbe({
      platform: 'darwin',
      runScript,
      isAccessibilityTrusted: () => true,
    });

    await expect(probe()).resolves.toBe(true);
    expect(runScript).toHaveBeenCalledTimes(1);
  });

  it('只检查 Codex 侧栏任务状态，不遍历整个 Chromium 窗口树', async () => {
    const runScript = vi.fn().mockResolvedValue('false\n');
    const probe = createCodexUiApprovalProbe({
      platform: 'darwin',
      runScript,
      isAccessibilityTrusted: () => true,
    });

    await expect(probe()).resolves.toBe(false);
    const script = runScript.mock.calls[0]?.[0] ?? '';
    expect(script).toContain('等待批准');
    expect(script).toContain('Waiting for approval');
    expect(script).not.toContain('entire contents');
    expect(script).not.toContain('repeat with appWindow in windows of codexProcess');
  });

  it('非 macOS 返回 false，脚本不可用时返回未知', async () => {
    const unavailable = vi.fn().mockRejectedValue(new Error('not authorized'));
    const macProbe = createCodexUiApprovalProbe({
      platform: 'darwin',
      runScript: unavailable,
      isAccessibilityTrusted: () => true,
    });
    const linuxProbe = createCodexUiApprovalProbe({
      platform: 'linux',
      runScript: unavailable,
    });

    await expect(macProbe()).resolves.toBeUndefined();
    await expect(linuxProbe()).resolves.toBe(false);
    expect(unavailable).toHaveBeenCalledTimes(1);
  });

  it('辅助功能权限未授予时返回未知且不执行窗口检查', async () => {
    const runScript = vi.fn().mockResolvedValue('true');
    const probe = createCodexUiApprovalProbe({
      platform: 'darwin',
      runScript,
      isAccessibilityTrusted: () => false,
    });

    await expect(probe()).resolves.toBeUndefined();
    expect(runScript).not.toHaveBeenCalled();
  });

  it('AppleScript 超时或不可用时返回未知并报告错误', async () => {
    const error = new Error('ETIMEDOUT');
    const onError = vi.fn();
    const probe = createCodexUiApprovalProbe({
      platform: 'darwin',
      runScript: vi.fn().mockRejectedValue(error),
      isAccessibilityTrusted: () => true,
      onError,
    });

    await expect(probe()).resolves.toBeUndefined();
    expect(onError).toHaveBeenCalledWith(error);
  });
});

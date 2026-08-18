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

  it('在非 macOS 或脚本不可用时安全返回 false', async () => {
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

    await expect(macProbe()).resolves.toBe(false);
    await expect(linuxProbe()).resolves.toBe(false);
    expect(unavailable).toHaveBeenCalledTimes(1);
  });

  it('辅助功能权限未授予时不执行窗口检查', async () => {
    const runScript = vi.fn().mockResolvedValue('true');
    const probe = createCodexUiApprovalProbe({
      platform: 'darwin',
      runScript,
      isAccessibilityTrusted: () => false,
    });

    await expect(probe()).resolves.toBe(false);
    expect(runScript).not.toHaveBeenCalled();
  });
});

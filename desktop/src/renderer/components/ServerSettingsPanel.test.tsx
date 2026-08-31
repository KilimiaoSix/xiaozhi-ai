/** @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ServerSettingsPanel } from './ServerSettingsPanel';
import type { AppConfigResolution } from '../../shared/appConfig';

const gatewayMocks = vi.hoisted(() => ({
  get: vi.fn(),
  update: vi.fn(),
}));

vi.mock('../../services/config/configDesktopGateway', () => ({
  configDesktopGateway: gatewayMocks,
}));

const resolution = (
  overrides: Partial<AppConfigResolution> = {},
): AppConfigResolution => ({
  serverUrl: {
    value: 'http://127.0.0.1:8003',
    source: 'default',
    fileValue: '',
  },
  deviceId: { value: '', source: 'default', fileValue: '' },
  authToken: { value: '', source: 'default', fileValue: '' },
  ...overrides,
});

const flush = async (): Promise<void> => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};

describe('ServerSettingsPanel', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    gatewayMocks.get.mockReset();
    gatewayMocks.update.mockReset();
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const render = async (): Promise<void> => {
    await act(async () => root.render(<ServerSettingsPanel />));
    await act(flush);
  };

  const input = (id: string): HTMLInputElement => {
    const element = container.querySelector<HTMLInputElement>(`#${id}`);
    if (!element) throw new Error(`找不到输入框 ${id}`);
    return element;
  };

  const setValue = async (id: string, value: string): Promise<void> => {
    const element = input(id);
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      'value',
    )!.set!;
    await act(async () => {
      setter.call(element, value);
      element.dispatchEvent(new Event('input', { bubbles: true }));
    });
  };

  it('逐字段展示生效值与来源', async () => {
    gatewayMocks.get.mockResolvedValue(resolution({
      serverUrl: {
        value: 'http://192.168.1.20:8003',
        source: 'file',
        fileValue: 'http://192.168.1.20:8003',
      },
      deviceId: {
        value: 'dc:da:0c:26:9a:60',
        source: 'env',
        envVar: 'DESKPET_DEVICE_ID',
        fileValue: '',
      },
    }));

    await render();

    const text = container.textContent ?? '';
    expect(text).toContain('http://192.168.1.20:8003');
    expect(text).toContain('配置文件');
    expect(text).toContain('dc:da:0c:26:9a:60');
    expect(text).toContain('DESKPET_DEVICE_ID');
    // 没有 env 覆盖的字段不该出现覆盖提示
    expect(container.querySelectorAll('.config-override')).toHaveLength(1);
  });

  it('环境变量覆盖时明说界面改了要去掉环境变量才生效', async () => {
    gatewayMocks.get.mockResolvedValue(resolution({
      serverUrl: {
        value: 'http://10.0.0.1:8003',
        source: 'env',
        envVar: 'DESKPET_SERVER',
        fileValue: 'http://192.168.1.20:8003',
      },
    }));

    await render();

    const override = container.querySelector('.config-override')?.textContent ?? '';
    expect(override).toContain('DESKPET_SERVER');
    expect(override).toContain('去掉环境变量');
    // 输入框编辑的是文件那一层，展示的也应该是文件里的值
    expect(input('config-server-url').value).toBe('http://192.168.1.20:8003');
  });

  it('保存把三个字段写回配置中心，并用返回的生效值刷新界面', async () => {
    gatewayMocks.get.mockResolvedValue(resolution());
    gatewayMocks.update.mockResolvedValue(resolution({
      serverUrl: {
        value: 'http://192.168.1.20:8003',
        source: 'file',
        fileValue: 'http://192.168.1.20:8003',
      },
      deviceId: {
        value: 'esp32-01',
        source: 'file',
        fileValue: 'esp32-01',
      },
    }));
    await render();

    await setValue('config-server-url', 'http://192.168.1.20:8003');
    await setValue('config-device-id', 'esp32-01');
    await act(async () => {
      container.querySelector<HTMLButtonElement>('.config-save')?.click();
    });
    await act(flush);

    expect(gatewayMocks.update).toHaveBeenCalledWith({
      serverUrl: 'http://192.168.1.20:8003',
      deviceId: 'esp32-01',
      authToken: '',
    });
    const text = container.textContent ?? '';
    expect(text).toContain('http://192.168.1.20:8003');
    expect(text).toContain('esp32-01');
    expect(text).toContain('已保存');
  });

  it('令牌不明文上屏，只说配没配', async () => {
    gatewayMocks.get.mockResolvedValue(resolution({
      authToken: {
        value: 'super-secret-token',
        source: 'env',
        envVar: 'DESKPET_SERVER_AUTH_TOKEN',
        fileValue: '',
      },
    }));

    await render();

    expect(container.textContent).not.toContain('super-secret-token');
    expect(container.textContent).toContain('已配置');
    expect(input('config-auth-token').type).toBe('password');
  });

  it('保存失败时把原因显示出来，不假装成功', async () => {
    gatewayMocks.get.mockResolvedValue(resolution());
    gatewayMocks.update.mockRejectedValue(new Error('配置写入失败：磁盘只读'));
    await render();

    await act(async () => {
      container.querySelector<HTMLButtonElement>('.config-save')?.click();
    });
    await act(flush);

    expect(container.querySelector('.config-error')?.textContent)
      .toContain('配置写入失败：磁盘只读');
  });

  it('读取配置失败时给出可操作的错误而不是空面板', async () => {
    gatewayMocks.get.mockRejectedValue(new Error('配置读取失败'));

    await render();

    expect(container.querySelector('.config-error')?.textContent)
      .toContain('配置读取失败');
  });

  it('把生效配置回传给上层，用来点亮链路状态', async () => {
    gatewayMocks.get.mockResolvedValue(resolution({
      serverUrl: {
        value: 'http://192.168.1.20:8003',
        source: 'file',
        fileValue: 'http://192.168.1.20:8003',
      },
    }));
    const onResolved = vi.fn();

    await act(async () => root.render(<ServerSettingsPanel onResolved={onResolved} />));
    await act(flush);

    expect(onResolved).toHaveBeenCalledWith(expect.objectContaining({
      serverUrl: expect.objectContaining({ value: 'http://192.168.1.20:8003' }),
    }));
  });
});

import type { AppConfig, AppConfigResolution } from '../../shared/appConfig';

/**
 * 设置面板与主进程配置中心之间的唯一通道。
 *
 * 与 incidentDesktopGateway 同形：面板不直接碰 window.xiaofei，
 * 测试里 mock 掉这一层就够了。
 */
export const configDesktopGateway = {
  get: (): Promise<AppConfigResolution> => window.xiaofei.config.get(),
  update: (patch: Partial<AppConfig>): Promise<AppConfigResolution> =>
    window.xiaofei.config.update(patch),
};

export type ConfigDesktopGateway = typeof configDesktopGateway;

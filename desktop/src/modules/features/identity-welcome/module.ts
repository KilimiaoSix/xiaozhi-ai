import type { FeatureModule } from '../../core/types';

export const identityWelcomeModule: FeatureModule = {
  definition: {
    id: 'identity-welcome',
    code: 'FACE',
    title: '身份识别与情绪欢迎',
    summary: '检测到熟悉面孔后，用匹配当下状态的方式打招呼。',
    triggerLabel: '模拟识别',
    status: 'placeholder',
    tone: 'cyan',
  },
  async execute() {
    return {
      title: '识别到熟悉用户 · Mock',
      detail: '已预留人脸识别、用户画像和情绪欢迎适配入口。',
      tone: 'cyan',
      source: 'mock',
    };
  },
};

import type { WellbeingTestKind } from '../../wellbeing/contracts';

export const wellbeingDesktopGateway = {
  sendTest: (kind: WellbeingTestKind) => window.xiaofei.wellbeing.sendTest(kind),
};

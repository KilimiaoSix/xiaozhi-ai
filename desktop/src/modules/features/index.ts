import { FeatureRegistry } from '../core/registry';
import { awayMessagesModule } from './away-messages';
import { codingAgentStatusModule } from './coding-agent-status';
import { feishuBriefingModule } from './feishu-briefing';
import { focusModeModule } from './focus-mode';
import { gestureApprovalModule } from './gesture-approval';
import { identityWelcomeModule } from './identity-welcome';
import { incidentAssistantModule } from './incident-assistant';
import { returnSummaryModule } from './return-summary';

export const featureModules = [
  identityWelcomeModule,
  feishuBriefingModule,
  codingAgentStatusModule,
  gestureApprovalModule,
  focusModeModule,
  awayMessagesModule,
  returnSummaryModule,
  incidentAssistantModule,
];

export const featureRegistry = new FeatureRegistry(featureModules);

export const featureCatalog = featureRegistry
  .list()
  .map(({ definition }) => definition);

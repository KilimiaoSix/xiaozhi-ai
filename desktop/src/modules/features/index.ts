import { FeatureRegistry } from '../core/registry';
import { awayMessagesModule } from './away-messages';
import { awaySummaryModule } from './away-summary';
import { codingAgentStatusModule } from './coding-agent-status/module';
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
  // 返岗汇总接手了 return-summary 的位置：同一格子，从「模拟返岗」换成真实面板入口
  awaySummaryModule,
  incidentAssistantModule,
  returnSummaryModule,
];

export const featureRegistry = new FeatureRegistry(featureModules);

export const featureCatalog = featureRegistry
  .list()
  .map(({ definition }) => definition);

/**
 * 仍在注册表里、但不再上屏的 Mock 占位模块。
 *
 * 它们的 execute 返回的是编好的假结果（`source: 'mock'`），点一下就往事件流里
 * 塞一条看着像真的记录——演示时容易被当成系统真的做了什么。按 AGENTS.md
 * 「模拟事件可以用于开发和演示，但必须明确标注为模拟或回放数据」，与其在卡片上
 * 补一堆「这是假的」，不如先从界面收起来；代码保留，等对应链路做实时再放出来。
 */
export const HIDDEN_FEATURE_IDS: ReadonlySet<string> = new Set([
  'identity-welcome',
  'gesture-approval',
  'away-messages',
  'return-summary',
]);

/** 界面用的目录：只留已接入真实链路的能力卡片。 */
export const visibleFeatureCatalog = featureCatalog.filter(
  (feature) => !HIDDEN_FEATURE_IDS.has(feature.id),
);

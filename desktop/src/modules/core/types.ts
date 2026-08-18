import type { FeatureDefinition, FeatureTone } from '../../shared/features';
import type { ServerGateway } from '../../services/server/serverGateway';

export interface FeatureExecutionResult {
  title: string;
  detail: string;
  tone: FeatureTone;
  source: 'mock' | 'live';
}

export interface FeatureRuntimeContext {
  now: () => Date;
  server: ServerGateway;
}

export interface FeatureModule {
  definition: FeatureDefinition;
  execute: (context: FeatureRuntimeContext) => Promise<FeatureExecutionResult>;
}

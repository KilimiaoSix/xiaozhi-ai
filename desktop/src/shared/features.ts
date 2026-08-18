export type FeatureStatus = 'placeholder' | 'ready' | 'attention';

export type FeatureTone = 'cyan' | 'violet' | 'amber' | 'coral';

export interface FeatureDefinition {
  id: string;
  code: string;
  title: string;
  summary: string;
  triggerLabel: string;
  status: FeatureStatus;
  tone: FeatureTone;
}

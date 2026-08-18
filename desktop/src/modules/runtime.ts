import type { FeatureRuntimeContext } from './core/types';
import { PlaceholderServerGateway } from '../services/server/placeholderServerGateway';

export const serverGateway = new PlaceholderServerGateway();

export const featureRuntimeContext: FeatureRuntimeContext = {
  now: () => new Date(),
  server: serverGateway,
};

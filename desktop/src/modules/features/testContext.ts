import type { FeatureRuntimeContext } from '../core/types';
import { PlaceholderServerGateway } from '../../services/server/placeholderServerGateway';

export const createFeatureTestContext = (): FeatureRuntimeContext => ({
  now: () => new Date('2026-08-18T08:00:00+08:00'),
  server: new PlaceholderServerGateway('http://192.168.1.2:8003'),
});

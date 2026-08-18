import type {
  FeatureExecutionResult,
  FeatureModule,
  FeatureRuntimeContext,
} from './types';

export class FeatureRegistry {
  private readonly modules: FeatureModule[];
  private readonly modulesById: Map<string, FeatureModule>;

  constructor(modules: FeatureModule[]) {
    this.modules = [...modules];
    this.modulesById = new Map();

    for (const featureModule of modules) {
      const { id } = featureModule.definition;
      if (this.modulesById.has(id)) {
        throw new Error(`功能模块 ID 重复: ${id}`);
      }
      this.modulesById.set(id, featureModule);
    }
  }

  list(): FeatureModule[] {
    return [...this.modules];
  }

  get(id: string): FeatureModule {
    const featureModule = this.modulesById.get(id);
    if (!featureModule) {
      throw new Error(`未找到功能模块: ${id}`);
    }
    return featureModule;
  }

  async execute(
    id: string,
    context: FeatureRuntimeContext,
  ): Promise<FeatureExecutionResult> {
    return this.get(id).execute(context);
  }
}

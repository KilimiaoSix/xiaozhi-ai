import type {
  AgentHookSourceDefinition,
  OwnedHookCommandOptions,
} from './types';

export const OWNED_HOOK_MARKER = 'launchcrush-agent-hook';

type JsonRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is JsonRecord =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);

const shellQuote = (value: string): string => `'${value.replaceAll("'", `'"'"'`)}'`;

export const createOwnedHookCommand = (
  options: OwnedHookCommandOptions,
): string => [
  shellQuote(options.launcherPath),
  '--owner',
  OWNED_HOOK_MARKER,
  '--source',
  options.source,
  '--spool',
  shellQuote(options.spoolPath),
].join(' ');

const handlerIsOwned = (value: unknown): boolean =>
  isRecord(value)
  && typeof value.command === 'string'
  && value.command.includes(`--owner ${OWNED_HOOK_MARKER}`);

const groupHasOwnedHandler = (value: unknown): boolean =>
  isRecord(value)
  && Array.isArray(value.hooks)
  && value.hooks.some(handlerIsOwned);

const hookTimeout = (
  definition: AgentHookSourceDefinition,
  eventName: string,
): number =>
  definition.source === 'codex' && eventName === 'SessionEnd' ? 3 : 5;

const groupHasOwnedCommand = (
  value: unknown,
  command: string,
  timeout: number,
): boolean =>
  isRecord(value)
  && Array.isArray(value.hooks)
  && value.hooks.some((handler) =>
    handlerIsOwned(handler)
    && handler.command === command
    && handler.timeout === timeout);

const refreshOwnedHandlers = (
  groups: unknown[],
  command: string,
  timeout: number,
): unknown[] =>
  groups.map((group) => {
    if (!isRecord(group) || !Array.isArray(group.hooks)) return group;
    return {
      ...group,
      hooks: group.hooks.map((handler) =>
        handlerIsOwned(handler) ? { ...handler, command, timeout } : handler),
    };
  });

const configClone = (value: unknown): JsonRecord => {
  if (!isRecord(value)) {
    throw new Error('Hook 配置根节点必须是 JSON 对象');
  }
  return structuredClone(value);
};

export const mergeOwnedHooks = (
  existing: unknown,
  definition: AgentHookSourceDefinition,
  command: string,
): JsonRecord => {
  const config = configClone(existing);
  if ('hooks' in config && !isRecord(config.hooks)) {
    throw new Error('Hook 配置中的 hooks 必须是 JSON 对象');
  }
  const hooks: JsonRecord = isRecord(config.hooks) ? config.hooks : {};

  for (const eventName of definition.events) {
    const timeout = hookTimeout(definition, eventName);
    const current = hooks[eventName];
    if (current !== undefined && !Array.isArray(current)) {
      throw new Error(`${eventName} Hook 配置必须是数组`);
    }
    const groups = refreshOwnedHandlers(
      Array.isArray(current) ? current : [],
      command,
      timeout,
    );
    if (groups.some(groupHasOwnedHandler)) {
      hooks[eventName] = groups;
      continue;
    }

    groups.push({
      ...(definition.toolEvents.includes(eventName) ? { matcher: '.*' } : {}),
      hooks: [{ type: 'command', command, timeout }],
    });
    hooks[eventName] = groups;
  }

  config.hooks = hooks;
  return config;
};

export const unmergeOwnedHooks = (existing: unknown): JsonRecord => {
  const config = configClone(existing);
  if (!isRecord(config.hooks)) return config;

  const nextHooks: JsonRecord = {};
  for (const [eventName, rawGroups] of Object.entries(config.hooks)) {
    if (!Array.isArray(rawGroups)) {
      nextHooks[eventName] = rawGroups;
      continue;
    }

    const groups = rawGroups.flatMap((rawGroup): unknown[] => {
      if (!isRecord(rawGroup) || !Array.isArray(rawGroup.hooks)) return [rawGroup];
      const handlers = rawGroup.hooks.filter((handler) => !handlerIsOwned(handler));
      return handlers.length > 0 ? [{ ...rawGroup, hooks: handlers }] : [];
    });
    if (groups.length > 0) nextHooks[eventName] = groups;
  }

  if (Object.keys(nextHooks).length > 0) {
    config.hooks = nextHooks;
  } else {
    delete config.hooks;
  }
  return config;
};

export const hasOwnedHooks = (existing: unknown): boolean =>
  isRecord(existing)
  && isRecord(existing.hooks)
  && Object.values(existing.hooks).some((groups) =>
    Array.isArray(groups) && groups.some(groupHasOwnedHandler));

export const ownedHooksMatchCommand = (
  existing: unknown,
  definition: AgentHookSourceDefinition,
  command: string,
): boolean => {
  if (!isRecord(existing) || !isRecord(existing.hooks)) return false;
  const hooks = existing.hooks;
  return definition.events.every((eventName) => {
    const groups = hooks[eventName];
    return Array.isArray(groups)
      && groups.some((group) =>
        groupHasOwnedCommand(
          group,
          command,
          hookTimeout(definition, eventName),
        ));
  });
};

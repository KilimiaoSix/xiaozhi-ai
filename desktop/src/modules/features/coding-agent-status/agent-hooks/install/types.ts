import type { AgentSource } from '../contracts';

export interface AgentHookSourceDefinition {
  source: AgentSource;
  commandNames: string[];
  events: string[];
  toolEvents: string[];
}

export interface AgentHookDetection {
  source: AgentSource;
  available: boolean;
  installed: boolean;
  executablePath?: string;
  configPath: string;
  lastEventAt?: string;
  message: string;
}

export interface AgentHookInstallResult {
  source: AgentSource;
  ok: boolean;
  installed: boolean;
  configPath: string;
  backupPath?: string;
  message: string;
}

export interface OwnedHookCommandOptions {
  electronPath: string;
  runnerPath: string;
  spoolPath: string;
  source: AgentSource;
}


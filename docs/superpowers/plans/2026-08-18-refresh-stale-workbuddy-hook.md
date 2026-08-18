# Refresh Stale WorkBuddy Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect and repair LaunchCrush-owned WorkBuddy Hook commands whose executable path became stale after a worktree was removed.

**Architecture:** Keep Hook ownership and config merging in `jsonHookConfig.ts`. Make merging replace owned handler commands with the current generated command, and make `AgentHookManager` distinguish a current installation from a stale owned installation before deciding whether a config write is needed.

**Tech Stack:** TypeScript, Electron Forge, Vitest

**Spec:** Root-cause evidence from the 2026-08-18 WorkBuddy diagnosis in this task; project boundaries in `AGENTS.md`.

## Global Constraints

- Preserve all non-LaunchCrush hooks and unknown configuration fields.
- Do not capture or persist additional prompt or conversation content.
- Hook failures must not block WorkBuddy.
- Simulated verification events must be explicitly marked as simulated.
- Preserve unrelated working-tree changes and do not create a commit unless requested.

---

### Task 1: Refresh stale owned commands

**Files:**
- Modify: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/jsonHookConfig.test.ts`
- Modify: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/jsonHookConfig.ts`

**Interfaces:**
- Consumes: `mergeOwnedHooks(existing, definition, command)` and LaunchCrush's `--owner launchcrush-agent-hook` marker.
- Produces: `ownedHooksMatchCommand(existing, definition, command): boolean`; merging replaces stale owned commands while preserving user handlers.

- [ ] Add a regression test with a deleted-worktree command and a user `Stop` hook; expect the stale path to disappear, the current command to appear once per configured event, and the user hook to remain.
- [ ] Run `npm test -- jsonHookConfig.test.ts` and confirm the stale path assertion fails before production changes.
- [ ] Update owned-handler merging and add `ownedHooksMatchCommand` with the smallest implementation that satisfies the behavior.
- [ ] Re-run `npm test -- jsonHookConfig.test.ts` and confirm it passes.

### Task 2: Report stale installations and repair on install

**Files:**
- Modify: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/manager.test.ts`
- Modify: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/manager.ts`

**Interfaces:**
- Consumes: `ownedHooksMatchCommand`, `createOwnedHookCommand`, and `mergeOwnedHooks`.
- Produces: detection result `installed: false` with an expired-path message for stale configs; `install(source)` rewrites stale commands and creates a backup while leaving current configs untouched.

- [ ] Add a manager regression test that seeds an old WorkBuddy command, verifies detection reports it stale, runs install, and verifies the current Electron path replaces it without removing user hooks.
- [ ] Run `npm test -- manager.test.ts` and confirm it fails for the stale-install behavior.
- [ ] Change manager detection and installation to compare against the expected command and rewrite only when needed.
- [ ] Re-run the focused manager test and both install test files.

### Task 3: Verify and migrate the live WorkBuddy configuration

**Files:**
- Runtime migration: `/Users/shishuangqi/.workbuddy/settings.json` through `AgentHookManager.install('workbuddy')`.
- Temporary verification spool: an isolated directory under `/private/tmp`.

**Interfaces:**
- Consumes: current project Electron executable and installed Hook runner.
- Produces: all WorkBuddy Hook commands reference the current checkout; a simulated `Stop` event is written successfully to an isolated spool.

- [ ] Run `npm test`, `npm run typecheck`, and `npm run package` in `desktop/`.
- [ ] Invoke the repaired manager against the live WorkBuddy config, preserving its automatic timestamped backup.
- [ ] Verify every LaunchCrush-owned WorkBuddy handler uses the current Electron path and no deleted-worktree path remains.
- [ ] Execute the resulting runner with a payload labeled `SIMULATED_WORKBUDDY_VERIFICATION` against an isolated temporary spool and verify one valid event file is produced.
- [ ] Restart the running Electron main process and confirm the App detects WorkBuddy as monitored.

## Self-Review

- Spec coverage: stale detection, safe migration, user-hook preservation, live config repair, and end-to-end event write are covered.
- Placeholder scan: no deferred implementation steps remain.
- Type consistency: helper and manager signatures match the existing synchronous config helpers and asynchronous manager API.

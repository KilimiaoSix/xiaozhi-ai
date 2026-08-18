import { execFile } from 'node:child_process';

type ScriptRunner = (script: string) => Promise<string>;

interface CreateCodexUiApprovalProbeOptions {
  platform?: NodeJS.Platform;
  runScript?: ScriptRunner;
  isAccessibilityTrusted?: () => boolean;
  onError?: (error: unknown) => void;
}

const CODEX_APPROVAL_SCRIPT = String.raw`
tell application "System Events"
  try
    set codexProcesses to every application process whose bundle identifier is "com.openai.codex"
    repeat with codexProcess in codexProcesses
      set mainWindows to every window of codexProcess whose name is "ChatGPT"
      repeat with appWindow in mainWindows
        -- Reach the sidebar project list without expanding the full Chromium AX tree.
        set currentItem to first UI element of appWindow whose role is "AXGroup"
        repeat 7 times
          set currentItem to first UI element of currentItem
        end repeat

        set currentItem to UI element 1 of currentItem
        set currentItem to UI element 2 of currentItem
        repeat 4 times
          set currentItem to first UI element of currentItem
        end repeat
        set currentItem to UI element 2 of currentItem
        set currentItem to UI element 4 of currentItem
        set projectList to UI element 4 of currentItem
        set inspectedTaskCount to 0

        repeat with projectContainer in UI elements of projectList
          try
            set projectContent to first UI element of projectContainer
            if role of projectContent is "AXGroup" and (count of UI elements of projectContent) >= 3 then
              set taskList to UI element 3 of projectContent
              if role of taskList is "AXList" then
                repeat with taskContainer in UI elements of taskList
                  try
                    set taskButton to first UI element of taskContainer
                    set taskButton to first UI element of taskButton
                    if role of taskButton is "AXButton" then
                      set inspectedTaskCount to inspectedTaskCount + 1
                      -- Codex exposes approval state as a direct child of the task button.
                      if exists static text "等待批准" of taskButton then return "true"
                      if exists static text "Waiting for approval" of taskButton then return "true"
                    end if
                  end try
                end repeat
              end if
            end if
          end try
        end repeat

        if inspectedTaskCount > 0 then return "false"
      end repeat
    end repeat
  end try
end tell
return "unavailable"
`;

const runAppleScript: ScriptRunner = (script) => new Promise((resolve, reject) => {
  execFile(
    '/usr/bin/osascript',
    ['-e', script],
    { timeout: 7_500, maxBuffer: 8_192 },
    (error, stdout) => {
      if (error) reject(error);
      else resolve(stdout);
    },
  );
});

export const createCodexUiApprovalProbe = (
  options: CreateCodexUiApprovalProbeOptions = {},
): (() => Promise<boolean | undefined>) => {
  const platform = options.platform ?? process.platform;
  const runScript = options.runScript ?? runAppleScript;
  const isAccessibilityTrusted = options.isAccessibilityTrusted ?? (() => true);
  let errorActive = false;

  return async () => {
    if (platform !== 'darwin') return false;
    if (!isAccessibilityTrusted()) return undefined;
    try {
      const result = (await runScript(CODEX_APPROVAL_SCRIPT)).trim();
      errorActive = false;
      if (result === 'true') return true;
      if (result === 'false') return false;
      return undefined;
    } catch (error) {
      if (!errorActive) options.onError?.(error);
      errorActive = true;
      return undefined;
    }
  };
};

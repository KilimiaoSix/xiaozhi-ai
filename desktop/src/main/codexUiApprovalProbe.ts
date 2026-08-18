import { execFile } from 'node:child_process';

type ScriptRunner = (script: string) => Promise<string>;

interface CreateCodexUiApprovalProbeOptions {
  platform?: NodeJS.Platform;
  runScript?: ScriptRunner;
  isAccessibilityTrusted?: () => boolean;
}

const CODEX_APPROVAL_SCRIPT = String.raw`
tell application "System Events"
  try
    set codexProcesses to every application process whose bundle identifier is "com.openai.codex"
    repeat with codexProcess in codexProcesses
      repeat with appWindow in windows of codexProcess
        set hasRiskTitle to false
        set hasDenyButton to false
        set hasAllowButton to false
        set windowContents to entire contents of appWindow
        repeat with uiItem in windowContents
          try
            set itemRole to role of uiItem
            set itemName to name of uiItem as text
            if itemRole is "AXStaticText" then
              if itemName starts with "允许 ChatGPT 使用" or itemName starts with "Allow ChatGPT to use" then
                set hasRiskTitle to true
              end if
            else if itemRole is "AXButton" then
              if itemName is "拒绝" or itemName is "Deny" then
                set hasDenyButton to true
              else if itemName is "允许此对话" or itemName is "始终允许" or itemName is "Allow this conversation" or itemName is "Allow for this conversation" or itemName is "Always allow" then
                set hasAllowButton to true
              end if
            end if
          end try
        end repeat
        if hasRiskTitle and hasDenyButton and hasAllowButton then return "true"
      end repeat
    end repeat
  end try
end tell
return "false"
`;

const runAppleScript: ScriptRunner = (script) => new Promise((resolve, reject) => {
  execFile(
    '/usr/bin/osascript',
    ['-e', script],
    { timeout: 1_500, maxBuffer: 8_192 },
    (error, stdout) => {
      if (error) reject(error);
      else resolve(stdout);
    },
  );
});

export const createCodexUiApprovalProbe = (
  options: CreateCodexUiApprovalProbeOptions = {},
): (() => Promise<boolean>) => {
  const platform = options.platform ?? process.platform;
  const runScript = options.runScript ?? runAppleScript;
  const isAccessibilityTrusted = options.isAccessibilityTrusted ?? (() => true);

  return async () => {
    if (platform !== 'darwin' || !isAccessibilityTrusted()) return false;
    try {
      return (await runScript(CODEX_APPROVAL_SCRIPT)).trim() === 'true';
    } catch {
      return false;
    }
  };
};

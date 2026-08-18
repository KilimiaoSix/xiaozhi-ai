export const HOOK_RUNNER_SOURCE = String.raw`'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === '--owner' || key === '--source' || key === '--spool') {
      result[key.slice(2)] = value;
    }
  }
  return result;
}

function diagnostic(spool, details) {
  try {
    const directory = path.join(spool || process.cwd(), 'diagnostics');
    fs.mkdirSync(directory, { recursive: true });
    fs.appendFileSync(
      path.join(directory, 'runner-errors.ndjson'),
      JSON.stringify(details) + '\n',
      'utf8',
    );
  } catch (_) {
    // Hook monitoring must never block the host Agent.
  }
}

function main(input) {
  const args = parseArgs(process.argv.slice(2));
  const receivedAt = new Date().toISOString();
  try {
    if (args.owner !== 'launchcrush-agent-hook') throw new Error('Invalid hook owner');
    if (!['codex', 'claude-code', 'workbuddy'].includes(args.source)) {
      throw new Error('Invalid hook source');
    }
    if (!args.spool) throw new Error('Missing spool path');

    const payload = JSON.parse(input);
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('Hook payload must be a JSON object');
    }

    const inbox = path.join(args.spool, 'inbox');
    fs.mkdirSync(inbox, { recursive: true });
    const identifier = crypto.randomUUID();
    const stem = receivedAt.replace(/[:.]/g, '-') + '-' + process.pid + '-' + identifier;
    const temporaryPath = path.join(inbox, stem + '.tmp');
    const eventPath = path.join(inbox, stem + '.json');
    fs.writeFileSync(temporaryPath, JSON.stringify({
      source: args.source,
      receivedAt,
      payload,
    }), 'utf8');
    fs.renameSync(temporaryPath, eventPath);
  } catch (error) {
    diagnostic(args.spool, {
      owner: args.owner,
      source: args.source,
      receivedAt,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => main(input));
process.stdin.resume();
`;


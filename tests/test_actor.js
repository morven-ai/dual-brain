#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const root = path.resolve(__dirname, '..');
const actorPath = path.join(root, 'hooks', 'lib', 'actor.js');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'dual-brain-actor-'));
const config = path.join(temp, 'config.json');
fs.writeFileSync(
  config,
  JSON.stringify({ proxyBaseUrl: 'http://127.0.0.1:8317/v1' }) + '\n',
  { mode: 0o600 }
);

for (const key of [
  'DUAL_BRAIN_ACTOR_MODEL',
  'ANTHROPIC_BASE_URL',
  'CLAUDE_CODE_SUBAGENT_MODEL',
  'CLAUDE_PLUGIN_DATA',
]) {
  delete process.env[key];
}
process.env.DUAL_BRAIN_CONFIG = config;
const actor = require(actorPath);

const transcript = (name, models) => {
  const file = path.join(temp, name);
  const lines = models.map(({ model, sidechain = false }) =>
    JSON.stringify({ type: 'assistant', isSidechain: sidechain, message: { model } })
  );
  fs.writeFileSync(file, `${lines.join('\n')}\n`);
  return file;
};

const resumedClaude = transcript('resumed-claude.jsonl', [
  ...Array(20).fill({ model: 'gpt-5.6-sol' }),
  { model: 'claude-opus-5' },
]);
assert.strictEqual(actor.modelFromTranscript(resumedClaude), 'claude-opus-5');
assert.strictEqual(actor.resolveActorModel({ transcript_path: resumedClaude }), 'claude-opus-5');

const resumedGpt = transcript('resumed-gpt.jsonl', [
  { model: 'claude-opus-5' },
  { model: 'gpt-5.6-sol' },
]);
assert.strictEqual(actor.isGptActor({ transcript_path: resumedGpt }), true);

const sidechain = transcript('sidechain.jsonl', [
  { model: 'claude-sonnet-5' },
  { model: 'gpt-5.6-luna', sidechain: true },
]);
assert.strictEqual(actor.resolveActorModel({ transcript_path: sidechain }), 'claude-sonnet-5');

process.env.DUAL_BRAIN_ACTOR_MODEL = 'gpt-5.6-sol';
assert.strictEqual(actor.resolveActorModel({ transcript_path: resumedClaude }), 'gpt-5.6-sol');
process.env.DUAL_BRAIN_ACTOR_MODEL = 'claude-opus-5';
assert.strictEqual(actor.resolveActorModel({ transcript_path: resumedGpt }), 'claude-opus-5');

delete process.env.DUAL_BRAIN_ACTOR_MODEL;
process.env.ANTHROPIC_BASE_URL = 'http://127.0.0.1:8317';
assert.strictEqual(actor.resolveActorModel({ transcript_path: resumedClaude }), 'gpt-proxy');
assert.strictEqual(actor.isGptActor({ transcript_path: resumedClaude }), true);
assert.strictEqual(actor.isConfiguredProxyBaseUrl('http://127.0.0.1:8317/other'), true);
assert.strictEqual(actor.isConfiguredProxyBaseUrl('https://127.0.0.1:8317'), false);
assert.strictEqual(actor.isConfiguredProxyBaseUrl('http://127.0.0.1:83170'), false);

process.env.ANTHROPIC_BASE_URL = 'http://127.0.0.1:83170';
assert.strictEqual(actor.resolveActorModel({ transcript_path: resumedClaude }), '');

delete process.env.ANTHROPIC_BASE_URL;
process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'gpt-5.6-sol';
assert.strictEqual(actor.resolveActorModel({}), '');

delete process.env.DUAL_BRAIN_CONFIG;
process.env.ANTHROPIC_BASE_URL = 'http://127.0.0.1:8317';
assert.strictEqual(actor.resolveActorModel({ transcript_path: resumedClaude }), '');
assert.strictEqual(actor.isGptActor({}), false);

fs.rmSync(temp, { recursive: true, force: true });
console.log('ok 16');

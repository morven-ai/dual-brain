'use strict';

/** Portable actor model 판정 SSOT. */

const fs = require('fs');
const path = require('path');

const TAIL_BYTES = 512 * 1024;

function configPath() {
  if (process.env.DUAL_BRAIN_CONFIG) return process.env.DUAL_BRAIN_CONFIG;
  if (process.env.CLAUDE_PLUGIN_DATA) {
    return path.join(process.env.CLAUDE_PLUGIN_DATA, 'config.json');
  }
  return '';
}

function loadConfig() {
  const file = configPath();
  if (!file) return null;
  try {
    const value = JSON.parse(fs.readFileSync(file, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function endpoint(raw) {
  if (!raw || typeof raw !== 'string') return null;
  try {
    const value = new URL(raw);
    if (!['http:', 'https:'].includes(value.protocol) || !value.hostname) return null;
    const port = value.port || (value.protocol === 'https:' ? '443' : '80');
    return { protocol: value.protocol, hostname: value.hostname.toLowerCase(), port };
  } catch {
    return null;
  }
}

function configuredProxyEndpoint(config = loadConfig()) {
  if (!config) return null;
  return endpoint(config.proxyBaseUrl || config.proxyEndpoint || '');
}

function isConfiguredProxyBaseUrl(raw, config = loadConfig()) {
  const observed = endpoint(raw);
  const expected = configuredProxyEndpoint(config);
  return Boolean(
    observed &&
      expected &&
      observed.protocol === expected.protocol &&
      observed.hostname === expected.hostname &&
      observed.port === expected.port
  );
}

function readTail(file, bytes) {
  const fd = fs.openSync(file, 'r');
  try {
    const size = fs.fstatSync(fd).size;
    const length = Math.min(size, bytes);
    const buffer = Buffer.alloc(length);
    fs.readSync(fd, buffer, 0, length, size - length);
    return buffer.toString('utf8');
  } finally {
    fs.closeSync(fd);
  }
}

/** 마지막 root assistant 응답을 고르고 sidechain·synthetic 응답은 제외한다. */
function modelFromTranscript(transcriptPath) {
  try {
    if (!transcriptPath || !fs.existsSync(transcriptPath)) return '';
    const lines = readTail(transcriptPath, TAIL_BYTES).split('\n');
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      const line = lines[index].trim();
      if (!line || line[0] !== '{') continue;
      let entry;
      try {
        entry = JSON.parse(line);
      } catch {
        continue;
      }
      if (entry.type !== 'assistant' || entry.isSidechain === true) continue;
      const model = entry.message && entry.message.model;
      if (!model || model === '<synthetic>') continue;
      return model;
    }
    return '';
  } catch {
    return '';
  }
}

/** explicit actor → 마지막 root transcript → exact configured proxy transport 순서다. */
function resolveActorModel(payload) {
  const explicit = process.env.DUAL_BRAIN_ACTOR_MODEL || '';
  const baseUrl = process.env.ANTHROPIC_BASE_URL || '';
  const localProxy = isConfiguredProxyBaseUrl(baseUrl);
  const ambiguousTransport = Boolean(baseUrl) && !localProxy;

  if (explicit) {
    if (/^gpt-/i.test(explicit)) return explicit;
    if (localProxy) return 'gpt-proxy';
    if (ambiguousTransport) return '';
    return explicit;
  }

  const transcriptModel = modelFromTranscript(payload && payload.transcript_path);
  if (transcriptModel) {
    if (/^gpt-/i.test(transcriptModel)) return transcriptModel;
    if (localProxy) return 'gpt-proxy';
    if (ambiguousTransport) return '';
    return transcriptModel;
  }

  if (localProxy) return 'gpt-proxy';
  return '';
}

function isGptActor(payload) {
  return /^gpt-/i.test(resolveActorModel(payload));
}

module.exports = {
  configuredProxyEndpoint,
  endpoint,
  isConfiguredProxyBaseUrl,
  isGptActor,
  modelFromTranscript,
  resolveActorModel,
};

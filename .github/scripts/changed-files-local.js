#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const micromatch = require('micromatch');
const YAML = require('yaml');

function getArg(name, defaultValue = '') {
  const idx = process.argv.indexOf(name);
  if (idx === -1 || idx + 1 >= process.argv.length) {
    return defaultValue;
  }
  return process.argv[idx + 1];
}

function flattenPatterns(patterns) {
  if (!patterns || !Array.isArray(patterns)) return [];
  return patterns.flat(Infinity).filter((p) => typeof p === 'string');
}

function matchFile(file, patterns) {
  const flat = flattenPatterns(patterns);
  if (flat.length === 0) return false;

  const positive = flat.filter((p) => !p.startsWith('!'));
  const negative = flat
    .filter((p) => p.startsWith('!'))
    .map((p) => p.slice(1));

  const matchesPositive = micromatch.isMatch(file, positive);
  const matchesNegative =
    negative.length > 0 && micromatch.isMatch(file, negative);

  return matchesPositive && !matchesNegative;
}

function getChangedFiles(baseSha, headSha) {
  let diffCmd;
  if (baseSha) {
    diffCmd = `git diff --name-only ${baseSha}..${headSha}`;
  } else {
    diffCmd = `git diff --name-only ${headSha}~1..${headSha}`;
  }

  const out = execSync(diffCmd, { encoding: 'utf8' });
  return out
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
}

function writeOutput(outputPath, key, value) {
  fs.appendFileSync(outputPath, `${key}=${value}\n`);
}

function main() {
  const filtersPath = getArg('--filters');
  const baseSha = getArg('--base-sha');
  const headSha = getArg('--head-sha', 'HEAD');
  const outputPath = getArg('--output', process.env.GITHUB_OUTPUT || '');

  if (!filtersPath) {
    console.error('Missing --filters <path>');
    process.exit(1);
  }

  if (!outputPath) {
    console.error('Missing --output <path> and GITHUB_OUTPUT is not set');
    process.exit(1);
  }

  const resolvedFilters = path.resolve(filtersPath);
  const filtersRaw = fs.readFileSync(resolvedFilters, 'utf8');
  const filters = YAML.parse(filtersRaw);

  let changedFiles = [];
  try {
    changedFiles = getChangedFiles(baseSha, headSha);
  } catch (err) {
    console.error(`Failed to compute changed files: ${err.message}`);
    process.exit(1);
  }

  const sortedChanged = [...new Set(changedFiles)].sort();
  const changedKeys = [];

  writeOutput(outputPath, 'all_all_modified_files', sortedChanged.join(' '));
  writeOutput(outputPath, 'all_all_modified_files_count', String(sortedChanged.length));

  for (const [key, patterns] of Object.entries(filters)) {
    const matched = sortedChanged.filter((file) => matchFile(file, patterns));
    const any = matched.length > 0;

    writeOutput(outputPath, `${key}_any_modified`, any ? 'true' : 'false');
    writeOutput(outputPath, `${key}_all_modified_files`, matched.join(' '));

    if (any && key !== 'all') {
      changedKeys.push(key);
    }
  }

  writeOutput(outputPath, 'changed_keys', changedKeys.join(','));

  console.log(`Changed files (${sortedChanged.length}):`);
  for (const f of sortedChanged) {
    console.log(`  - ${f}`);
  }
  console.log(`Matched filters: ${changedKeys.join(', ') || '(none)'}`);
}

main();

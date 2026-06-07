#!/usr/bin/env node
// Bump the version in package.json AND pyproject.toml together so the npm and
// PyPI packages always ship in lockstep. publish.yml's gate refuses to publish
// if the two (and the git tag) disagree, so this script is the easy way to keep
// them in sync.
//
//   node scripts/bump-version.mjs 0.2.0
//
import { readFileSync, writeFileSync } from 'node:fs';
import process from 'node:process';

const version = process.argv[2];
if (!version || !/^\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?$/.test(version)) {
  console.error('Usage: node scripts/bump-version.mjs <version>   e.g. 0.2.0 or 0.2.0-rc.1');
  process.exit(1);
}

const pkgUrl = new URL('../package.json', import.meta.url);
const pkg = JSON.parse(readFileSync(pkgUrl, 'utf8'));
const prev = pkg.version;
pkg.version = version;
writeFileSync(pkgUrl, JSON.stringify(pkg, null, 2) + '\n');

const pyUrl = new URL('../pyproject.toml', import.meta.url);
let py = readFileSync(pyUrl, 'utf8');
if (!/^version = ".*"$/m.test(py)) {
  console.error('Could not find `version = "..."` in pyproject.toml — aborting.');
  process.exit(1);
}
py = py.replace(/^version = ".*"$/m, `version = "${version}"`);
writeFileSync(pyUrl, py);

console.log(`version ${prev} -> ${version} (package.json + pyproject.toml)`);
console.log('\nNext:');
console.log(`  git commit -am "release: v${version}"`);
console.log(`  git tag v${version} && git push --follow-tags`);
console.log('\nThat tag push triggers .github/workflows/publish.yml (npm + PyPI + GHCR + Release).');

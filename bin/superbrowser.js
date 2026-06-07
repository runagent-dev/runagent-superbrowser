#!/usr/bin/env node
// CLI entry for the SuperBrowser TS engine.
//
// The compiled entry (build/index.js) reads its mode from process.argv[2]
// (http | mcp | task) and bootstraps itself, so this shim just loads it — Node
// forwards argv identically whether you run `node build/index.js <mode>` or
// `superbrowser <mode>`. Kept separate from build/index.js so the compiled
// output stays shebang-free and still importable as a library.
import '../build/index.js';

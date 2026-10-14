#!/usr/bin/env node
// Collect license texts for the viewer's production dependency tree and write
// viewer/dist/THIRD_PARTY_LICENSES.txt, so the wheel (which bundles viewer/dist)
// carries the notices its dependencies and fonts require. No extra dependencies:
// it reads `pnpm licenses list --prod --json` and the LICENSE files on disk.
//
// Run after `vite build` (the viewer `build` script does this). Usage:
//   node scripts/collect_viewer_licenses.mjs [viewerDir]

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const viewerDir = resolve(process.argv[2] ?? join(root, "viewer"));
const outFile = join(viewerDir, "dist", "THIRD_PARTY_LICENSES.txt");

// Call the pnpm that is running this build (`corepack pnpm run build` sets
// npm_execpath to its own entry script), not whichever pnpm is first on PATH:
// an older global pnpm rejects the lockfile.
const pnpmArgs = ["licenses", "list", "--prod", "--json"];
const execPath = process.env.npm_execpath ?? "";
const [cmd, args, shell] = /pnpm[^/\\]*\.[cm]?js$/i.test(execPath)
  ? [process.execPath, [execPath, ...pnpmArgs], false]
  : ["pnpm", pnpmArgs, process.platform === "win32"]; // pnpm is pnpm.cmd on Windows
const raw = execFileSync(cmd, args, {
  cwd: viewerDir,
  encoding: "utf8",
  maxBuffer: 64 * 1024 * 1024,
  shell,
});
const byLicense = JSON.parse(raw.slice(raw.indexOf("{")));

const LICENSE_FILE = /^(licen[cs]e|copying|notice)(\.|-|$)/i;
const licenseText = (dir) => {
  if (!existsSync(dir)) return null;
  const names = readdirSync(dir).filter((n) => LICENSE_FILE.test(n)).sort();
  return names.length ? names.map((n) => readFileSync(join(dir, n), "utf8").trim()).join("\n\n") : null;
};

const seen = new Set();
const entries = [];
const counts = {};
for (const [license, pkgs] of Object.entries(byLicense)) {
  for (const pkg of pkgs) {
    pkg.versions.forEach((version, i) => {
      const key = `${pkg.name}@${version}`;
      if (seen.has(key)) return;
      seen.add(key);
      counts[license] = (counts[license] ?? 0) + 1;
      entries.push({
        key,
        license,
        homepage: pkg.homepage,
        author: pkg.author,
        text: licenseText(pkg.paths[i] ?? pkg.paths[0]),
      });
    });
  }
}
entries.sort((a, b) => a.key.localeCompare(b.key));

const missing = entries.filter((e) => !e.text).map((e) => e.key);
const bar = "=".repeat(78);
const parts = [
  "agentplay viewer: third-party licenses",
  "",
  "The bundled web viewer includes the open-source packages below (production",
  "dependencies, including transitive ones, and bundled fonts). Each entry gives",
  "the declared SPDX license and the license text shipped by the package.",
  "",
  "Summary: " + Object.entries(counts).sort().map(([l, n]) => `${l} (${n})`).join(", "),
  "",
];
for (const e of entries) {
  parts.push(bar, `${e.key}  [${e.license}]`);
  if (e.author) parts.push(`Author: ${e.author}`);
  if (e.homepage) parts.push(`Homepage: ${e.homepage}`);
  parts.push("", e.text ?? `(no license file shipped; declared license: ${e.license})`, "");
}

mkdirSync(dirname(outFile), { recursive: true });
writeFileSync(outFile, parts.join("\n"));
console.log(`wrote ${outFile}: ${entries.length} packages`);
for (const [l, n] of Object.entries(counts).sort()) console.log(`  ${l}: ${n}`);
if (missing.length) console.log(`  no license file on disk (declared license only): ${missing.join(", ")}`);

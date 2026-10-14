#!/usr/bin/env node
/**
 * Serve `dist/` in demo mode and capture the explorer in light and dark
 * (prefers-color-scheme) into ../docs/images/explorer-{light,dark}.png. Fails on console errors.
 *
 *   pnpm build && node scripts/screenshots.mjs
 *
 * Needs a Chromium: set CHROMIUM_PATH, or it looks under
 * $PLAYWRIGHT_BROWSERS_PATH for a playwright-managed build.
 */
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIST = path.join(ROOT, 'dist');
const OUT = path.join(ROOT, '../docs/images');
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.woff2': 'font/woff2' };

function findChromium() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH ?? '/opt/pw-browsers';
  for (const d of fs.existsSync(base) ? fs.readdirSync(base).sort().reverse() : []) {
    if (!d.startsWith('chromium-')) continue;
    for (const rel of ['chrome-linux/chrome', 'chrome-mac/Chromium.app/Contents/MacOS/Chromium', 'chrome-win/chrome.exe']) {
      const p = path.join(base, d, rel);
      if (fs.existsSync(p)) return p;
    }
  }
  return undefined;
}

// A static server that, like a plain file host, has no /api (so the viewer
// falls back to demo mode on its own).
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  let file = path.join(DIST, decodeURIComponent(url.pathname));
  if (file.endsWith('/')) file = path.join(file, 'index.html');
  if (!file.startsWith(DIST) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end('not found');
    return;
  }
  res.writeHead(200, { 'content-type': TYPES[path.extname(file)] ?? 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;
const sessionId = process.env.SESSION ?? 'local:claude-code:5f2c9a1e-7b44-4d0e-9c1a-demo00000001';

fs.mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch({ executablePath: findChromium() });
const problems = [];
for (const scheme of ['light', 'dark']) {
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, colorScheme: scheme, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on('console', (m) => {
    if (m.type() === 'error' || m.type() === 'warning') problems.push(`[${scheme}] console.${m.type()}: ${m.text()}`);
  });
  page.on('pageerror', (e) => problems.push(`[${scheme}] pageerror: ${e.message}`));
  await page.goto(`http://127.0.0.1:${port}/?demo&session=${encodeURIComponent(sessionId)}${process.env.QUERY ?? ''}`);
  await page.waitForSelector('svg circle', { timeout: 15000 });
  await page.waitForSelector('canvas', { timeout: 15000 });
  await page.waitForTimeout(800);
  const file = path.join(OUT, `${process.env.PREFIX ?? 'explorer-'}${scheme}.png`);
  await page.screenshot({ path: file });
  console.log(`saved ${path.relative(ROOT, file)}`);
  await ctx.close();
}
await browser.close();
server.close();
if (problems.length) {
  console.error(problems.join('\n'));
  process.exit(1);
}

#!/usr/bin/env node
/**
 * Generate the bundled demo sessions: `public/demo/index.json` plus one
 * `agentplay/v1` map document per session.
 *
 * Everything here is SYNTHETIC: invented repos (acme/*), invented PR numbers
 * and hashes, no real transcript behind any of it. The output is fully
 * deterministic (seeded PRNG, fixed dates), so re-running the script produces
 * byte-identical files.
 *
 *   node scripts/make-demo.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const OUT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../public/demo');
const SCHEMA = 'agentplay/v1';

// --- deterministic PRNG (mulberry32) ----------------------------------------
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** $ per million tokens: fresh input, cache write, cache read, output. */
const PRICE = {
  'claude-opus-4-5': [5, 6.25, 0.5, 25],
  'claude-sonnet-4-5': [3, 3.75, 0.3, 15],
  'claude-haiku-4-5': [1, 1.25, 0.1, 5],
  'gpt-5-codex': [1.25, 0, 0.125, 10],
  'cursor-auto': [1.25, 0, 0.125, 6],
};

const NON_CALL = new Set([
  'llm', 'user', 'compaction', 'error', 'delay', 'model_switch',
  'commit', 'pr_merge', 'pr_close', 'pr_comment', 'ticket', 'artifact', 'skill_write',
]);

const CAT = {
  read: 'file', edit: 'file', search: 'file', exec: 'shell', verify: 'test',
  mcp: 'mcp', web: 'web', skill: 'skill', subagent: 'subagent', tool: 'tool',
};

class Session {
  constructor({ id, agentTool, startedAt, cliVersion, seed, repos }) {
    this.id = id;
    this.agentTool = agentTool;
    this.startedAt = startedAt;
    this.cliVersion = cliVersion;
    this.rand = rng(seed);
    this.t = 0;
    this.events = [];
    this.repos = repos.map((r, i) => ({ id: `r${i}`, name: r, remote: `git@github.com:${r}.git` }));
    this.files = [];
    this.agents = [{ id: 'main', parent: null, kind: 'main' }];
    this.prs = [];
    this.segments = [];
    this.model = { main: null };
  }

  /** advance the clock by `lo..hi` ms (deterministic) */
  tick(lo, hi) {
    this.t += Math.round(lo + this.rand() * (hi - lo));
    return this.t;
  }

  file(repo, p) {
    const r = this.repos.find((x) => x.name === repo);
    let f = this.files.find((x) => x.repo === r.id && x.path === p);
    if (!f) {
      f = { id: this.files.length, repo: r.id, path: p, reads: 0, edits: 0, hits: 0 };
      this.files.push(f);
    }
    return f.id;
  }

  push(ev, gap = [400, 2600]) {
    const e = { seq: this.events.length, t: this.tick(...gap), agent: 'main', err: false, ...ev };
    if (!NON_CALL.has(e.kind) && !e.cat) e.cat = e.vcs ? 'vcs' : CAT[e.kind] ?? 'tool';
    this.events.push(e);
    return e;
  }

  task(name) {
    this.segments.push({ name, start: this.events.length });
  }

  user(chars, extra = {}) {
    return this.push({ kind: 'user', chars, ...extra }, [4000, 20000]);
  }

  llm(model, { agent = 'main', ctx = 30000, out = 600, latency } = {}) {
    const prev = this.model[agent];
    if (prev && prev !== model && agent === 'main') {
      this.push({ kind: 'model_switch', agent, from: prev, to: model }, [0, 5]);
    }
    this.model[agent] = model;
    // Cursor does not write billed usage to disk, so its events carry zeros.
    const noUsage = this.agentTool === 'cursor';
    const cached = Math.round(ctx * (0.72 + this.rand() * 0.2));
    const cw = Math.round((ctx - cached) * 0.8);
    const fresh = ctx - cached - cw;
    const [pi, pw, pr, po] = PRICE[model];
    const cost = (fresh * pi + cw * pw + cached * pr + out * po) / 1e6;
    const lat = latency ?? Math.round(1800 + this.rand() * 5200 + out * 4);
    return this.push(
      {
        kind: 'llm',
        agent,
        model,
        in: noUsage ? 0 : fresh,
        cw: noUsage ? 0 : cw,
        cr: noUsage ? 0 : cached,
        out: noUsage ? 0 : out,
        cost: noUsage ? 0 : Math.round(cost * 1e5) / 1e5,
        latency: lat,
        stop: 'tool_use',
        toolCalls: 1,
        thinking: model.startsWith('claude') && this.rand() > 0.5,
        textChars: Math.round(out * 1.8),
      },
      [lat - 200, lat + 200]
    );
  }

  read(repo, p, agent = 'main', tool = 'Read') {
    return this.push({ kind: 'read', agent, tool, targets: [{ file: this.file(repo, p), touch: 'read' }] });
  }

  search(repo, paths, agent = 'main', verb = 'rg') {
    return this.push({
      kind: 'search',
      agent,
      tool: 'Grep',
      verb,
      targets: paths.map((p) => ({ file: this.file(repo, p), touch: 'hit' })),
    });
  }

  edit(repo, p, op = 'modify', agent = 'main', tool = 'Edit') {
    return this.push({
      kind: 'edit',
      agent,
      tool: op === 'create' ? 'Write' : tool,
      targets: [{ file: this.file(repo, p), touch: 'edit', op }],
    });
  }

  exec(verb, extra = {}) {
    return this.push({ kind: 'exec', tool: 'Bash', verb, ...extra }, [900, 6000]);
  }

  verify(verb, extra = {}) {
    return this.push({ kind: 'verify', tool: 'Bash', verb, cat: 'test', ...extra }, [3000, 16000]);
  }

  subagent(child, subagentType, model) {
    this.agents.push({ id: child, parent: 'main', kind: 'subagent', subagentType, launchSeq: this.events.length });
    return this.push({ kind: 'subagent', tool: 'Task', subagentType, child, model });
  }

  commit(repo, sha, files, ins, del) {
    this.exec('git', { vcs: { provider: 'git', action: 'commit', repo } });
    return this.push({ kind: 'commit', repo, sha, files, ins, del }, [50, 200]);
  }

  prCreate(repo, number) {
    const e = this.exec('gh', { vcs: { provider: 'github', action: 'pr_create', repo, number } });
    this.prs.push({ provider: 'github', repo, number, url: `https://github.com/${repo}/pull/${number}`, seq: e.seq, state: 'open' });
    return e;
  }

  finish({ outcome }) {
    const evs = this.events;
    // per-file touch counts and last op
    for (const e of evs) {
      for (const t of e.targets ?? []) {
        if (t.file == null) continue;
        const f = this.files[t.file];
        if (t.touch === 'edit') f.edits++;
        else if (t.touch === 'read') f.reads++;
        else f.hits++;
        if (f.first == null) f.first = e.seq;
        if (t.op) f.op = f.op === 'create' ? 'create' : t.op;
      }
    }
    // stamp the driving model on every event
    let cur = {};
    for (const e of evs) {
      if (e.kind === 'llm') cur[e.agent] = e.model;
      if (e.kind !== 'llm' && e.kind !== 'model_switch' && cur[e.agent] && !e.model) e.model = cur[e.agent];
    }
    // tasks from the named segments
    const tasks = this.segments.map((s, i) => {
      const end = (this.segments[i + 1]?.start ?? evs.length) - 1;
      return {
        n: i + 1,
        id: `task:${i + 1}`,
        name: s.name,
        source: 'segments',
        startSeq: s.start,
        endSeq: end,
        startT: evs[s.start].t,
        endT: evs[end].t,
      };
    });
    for (const task of tasks) for (let i = task.startSeq; i <= task.endSeq; i++) evs[i].task = task.n;

    const llm = evs.filter((e) => e.kind === 'llm');
    const agents = this.agents.map((a) => {
      const mine = evs.filter((e) => e.agent === a.id);
      return {
        ...a,
        start: mine[0].t,
        end: mine[mine.length - 1].t,
        calls: mine.filter((e) => !NON_CALL.has(e.kind)).length,
        requests: mine.filter((e) => e.kind === 'llm').length,
        tokens: mine.reduce((s, e) => s + (e.in ?? 0) + (e.cw ?? 0) + (e.cr ?? 0) + (e.out ?? 0), 0),
        cost: round4(mine.reduce((s, e) => s + (e.kind === 'llm' ? e.cost : 0), 0)),
        outcome: a.id === 'main' ? outcome : 'completed',
      };
    });
    const bySrc = {};
    const byCls = {};
    for (const e of evs) {
      if (!e.err) continue;
      const src = e.kind === 'error' ? e.source ?? 'tool' : e.kind === 'mcp' ? 'mcp' : e.kind === 'subagent' ? 'orchestration' : 'tool';
      bySrc[src] = (bySrc[src] ?? 0) + 1;
      const cls = e.errClass ?? 'other';
      byCls[cls] = (byCls[cls] ?? 0) + 1;
    }
    const lats = llm.map((e) => e.latency).sort((a, b) => a - b);
    const delays = evs.filter((e) => e.kind === 'delay');
    const ctx = llm.filter((e) => e.agent === 'main').map((e) => e.in + e.cw + e.cr);
    const models = [...new Set(llm.map((e) => e.model))].sort();
    const totals = {
      calls: evs.filter((e) => !NON_CALL.has(e.kind)).length,
      requests: llm.length,
      userTurns: evs.filter((e) => e.kind === 'user').length,
      subagents: agents.length - 1,
      errors: evs.filter((e) => e.err).length,
      tokens: llm.reduce((s, e) => s + e.in + e.cw + e.cr + e.out, 0),
      costUsd: round4(llm.reduce((s, e) => s + e.cost, 0)),
      filesTouched: this.files.length,
      prsCreated: this.prs.length,
      filesCreated: this.files.filter((f) => f.op === 'create').length,
      filesDeleted: this.files.filter((f) => f.op === 'delete').length,
      errorsBySource: bySrc,
      errorsByClass: byCls,
      retries: evs.filter((e) => e.retryOf != null).length,
      delays: delays.length,
      delayMs: delays.reduce((s, e) => s + e.ms, 0),
      maxContext: Math.max(0, ...ctx),
      compactions: evs.filter((e) => e.kind === 'compaction').length,
      llmLatencyP50: lats[Math.floor(lats.length / 2)] ?? 0,
      llmLatencyP95: lats[Math.min(lats.length - 1, Math.floor(lats.length * 0.95))] ?? 0,
      outputTokens: llm.reduce((s, e) => s + e.out, 0),
      tasks: tasks.length,
      commits: evs.filter((e) => e.kind === 'commit').length,
    };
    for (const pr of this.prs) {
      if (evs.some((e) => e.kind === 'pr_merge' && e.number === pr.number)) pr.state = 'merged';
    }
    return {
      schema: SCHEMA,
      session: {
        id: this.id,
        agentTool: this.agentTool,
        cliVersion: this.cliVersion,
        startedAt: this.startedAt,
        durationMs: evs[evs.length - 1].t,
        models,
        pathMode: 'relative',
        truncated: false,
        outcome,
        role: 'root',
        parentSessionId: null,
      },
      totals,
      repos: this.repos,
      files: this.files,
      prs: this.prs,
      agents,
      tasks,
      events: evs,
    };
  }
}

const round4 = (n) => Math.round(n * 1e4) / 1e4;

// --- session 1: Claude Code, rate limiting in acme/api ----------------------
function claudeSession() {
  const s = new Session({
    id: 'local:claude-code:5f2c9a1e-7b44-4d0e-9c1a-demo00000001',
    agentTool: 'claude-code',
    startedAt: '2026-09-24T14:05:12.000Z',
    cliVersion: '2.1.4',
    seed: 11,
    repos: ['acme/api'],
  });
  const R = 'acme/api';
  const O = 'claude-opus-4-5';
  const H = 'claude-haiku-4-5';
  const S = 'claude-sonnet-4-5';

  s.task('Explore the request pipeline');
  s.user(1180, { urls: 1 });
  s.llm(O, { ctx: 18000, out: 420 });
  s.read(R, 'README.md');
  s.llm(O, { ctx: 21000, out: 380 });
  s.subagent('sub-explore', 'Explore', H);
  s.llm(H, { agent: 'sub-explore', ctx: 9000, out: 300 });
  s.search(R, ['src/middleware/index.ts', 'src/middleware/auth.ts', 'src/server.ts'], 'sub-explore');
  s.llm(H, { agent: 'sub-explore', ctx: 12000, out: 260 });
  s.read(R, 'src/middleware/index.ts', 'sub-explore');
  s.read(R, 'src/middleware/auth.ts', 'sub-explore');
  s.llm(H, { agent: 'sub-explore', ctx: 16000, out: 280 });
  s.read(R, 'src/server.ts', 'sub-explore');
  s.read(R, 'src/config/env.ts', 'sub-explore');
  s.llm(H, { agent: 'sub-explore', ctx: 21000, out: 900 });
  s.llm(O, { ctx: 26000, out: 520 });
  s.read(R, 'src/routes/orders.ts');
  s.read(R, 'package.json');
  s.push({ kind: 'mcp', server: 'docs', tool: 'search_docs', outBytes: 4200, dur: 820 });
  s.llm(O, { ctx: 31000, out: 640 });

  s.task('Add a token-bucket rate limiter');
  s.user(420);
  s.llm(O, { ctx: 72000, out: 1400 });
  s.edit(R, 'src/middleware/rateLimit.ts', 'create');
  s.llm(O, { ctx: 88000, out: 700 });
  s.edit(R, 'src/middleware/index.ts');
  s.edit(R, 'src/config/env.ts');
  s.llm(O, { ctx: 104000, out: 1100 });
  s.edit(R, 'test/rateLimit.test.ts', 'create');
  s.llm(O, { ctx: 117000, out: 300 });
  const failed = s.verify('npm test', { err: true, errClass: 'exit_code', exitCode: 1, dur: 14200 });
  s.llm(O, { ctx: 129000, out: 520 });
  s.read(R, 'src/middleware/rateLimit.ts');
  s.edit(R, 'src/middleware/rateLimit.ts');
  s.llm(O, { ctx: 138000, out: 260 });
  const pass = s.verify('npm test', { retryOf: failed.seq, attempt: 2, dur: 12800 });
  s.push({ kind: 'delay', cause: 'tool', ms: 12800, ref: pass.seq }, [50, 100]);
  s.push({ kind: 'error', source: 'llm', errClass: 'overloaded', err: true, retry: true }, [800, 1500]);
  s.llm(O, { ctx: 149000, out: 480, latency: 21000 });
  s.push({ kind: 'delay', cause: 'llm', ms: 21000, ref: s.events.length - 1 }, [50, 100]);
  s.exec('npx', { verb: 'tsc', dur: 6100 });
  s.llm(O, { ctx: 161000, out: 220 });
  s.edit(R, 'src/routes/orders.ts');
  s.edit(R, 'docs/rate-limits.md', 'create');

  s.task('Commit and open a PR');
  s.user(160);
  s.push({ kind: 'compaction', trigger: 'auto', preTokens: 168400, postTokens: 23100 }, [1200, 2000]);
  s.model.main = O;
  s.llm(S, { ctx: 24000, out: 380 });
  s.exec('git', { vcs: { provider: 'git', action: 'status', repo: R } });
  s.exec('git', { vcs: { provider: 'git', action: 'diff', repo: R } });
  s.llm(S, { ctx: 29000, out: 540 });
  s.commit(R, '9d41c07be2a1', 6, 214, 12);
  s.exec('git', { vcs: { provider: 'git', action: 'push', repo: R } });
  s.llm(S, { ctx: 31000, out: 820 });
  s.prCreate(R, 142);
  s.push({ kind: 'pr_comment', repo: R, number: 142, url: 'https://github.com/acme/api/pull/142' });
  s.llm(S, { ctx: 33000, out: 260, latency: 2600 });
  return {
    doc: s.finish({ outcome: 'pr_created' }),
    title: 'Demo: Add rate limiting to the orders API',
    file: 'claude-code-rate-limit.json',
  };
}

// --- session 2: Codex, flaky date test in acme/web --------------------------
function codexSession() {
  const s = new Session({
    id: 'local:codex:0199a3b2-demo-4f0c-8e55-000000000002',
    agentTool: 'codex',
    startedAt: '2026-09-23T09:41:03.000Z',
    cliVersion: '0.44.0',
    seed: 23,
    repos: ['acme/web', 'acme/ui-kit'],
  });
  const W = 'acme/web';
  const U = 'acme/ui-kit';
  const M = 'gpt-5-codex';
  s.task('Reproduce the flaky date test');
  s.user(640);
  s.llm(M, { ctx: 14000, out: 300 });
  s.exec('rg', { kind: 'search', tool: 'shell', targets: [
    { file: s.file(W, 'src/lib/formatDate.ts'), touch: 'hit' },
    { file: s.file(W, 'src/lib/formatDate.test.ts'), touch: 'hit' },
    { file: s.file(W, 'src/components/OrderRow.tsx'), touch: 'hit' },
  ] });
  s.llm(M, { ctx: 17000, out: 260 });
  s.read(W, 'src/lib/formatDate.ts', 'main', 'shell');
  s.read(W, 'src/lib/formatDate.test.ts', 'main', 'shell');
  s.llm(M, { ctx: 21000, out: 240 });
  const f1 = s.verify('pnpm vitest', { tool: 'shell', err: true, errClass: 'exit_code', exitCode: 1, dur: 9100 });
  s.llm(M, { ctx: 24000, out: 380 });
  s.exec('TZ=Pacific/Auckland pnpm', { tool: 'shell', verb: 'pnpm', err: true, errClass: 'exit_code', exitCode: 1 });
  s.llm(M, { ctx: 26000, out: 420 });
  s.read(U, 'src/date/index.ts', 'main', 'shell');
  s.push({ kind: 'web', tool: 'web_search', host: 'developer.mozilla.org' });
  s.llm(M, { ctx: 30000, out: 520 });

  s.task('Fix the timezone handling');
  s.user(210);
  s.llm(M, { ctx: 32000, out: 900 });
  s.edit(W, 'src/lib/formatDate.ts', 'modify', 'main', 'apply_patch');
  s.edit(W, 'src/lib/formatDate.test.ts', 'modify', 'main', 'apply_patch');
  s.llm(M, { ctx: 35000, out: 300 });
  s.verify('pnpm vitest', { tool: 'shell', retryOf: f1.seq, attempt: 2, dur: 8800 });
  s.llm(M, { ctx: 36000, out: 200 });
  s.verify('pnpm lint', { tool: 'shell', cat: 'build', dur: 7400 });
  s.llm(M, { ctx: 37000, out: 360 });
  s.commit(W, '3be07f19ac44', 2, 18, 7);
  s.llm(M, { ctx: 38000, out: 180, latency: 2100 });
  return {
    doc: s.finish({ outcome: 'verified' }),
    title: 'Demo: Fix a flaky timezone test',
    file: 'codex-flaky-test.json',
  };
}

// --- session 3: Cursor, short copy change -----------------------------------
function cursorSession() {
  const s = new Session({
    id: 'local:cursor:c1d2e3f4-demo-4a5b-9c8d-000000000003',
    agentTool: 'cursor',
    startedAt: '2026-09-22T17:20:44.000Z',
    cliVersion: '1.7',
    seed: 37,
    repos: ['acme/web'],
  });
  const W = 'acme/web';
  const M = 'cursor-auto';
  s.task('Update the empty-cart copy');
  s.user(180);
  s.llm(M, { ctx: 9000, out: 220 });
  s.search(W, ['src/pages/cart.tsx', 'src/i18n/en.json'], 'main', 'grep');
  s.read(W, 'src/i18n/en.json', 'main', 'read_file');
  s.llm(M, { ctx: 11000, out: 260 });
  s.edit(W, 'src/i18n/en.json', 'modify', 'main', 'edit_file');
  s.edit(W, 'src/pages/cart.tsx', 'modify', 'main', 'edit_file');
  s.llm(M, { ctx: 12500, out: 140, latency: 1900 });
  return {
    doc: s.finish({ outcome: 'completed' }),
    title: 'Demo: Update empty-cart copy',
    file: 'cursor-copy-tweak.json',
  };
}

// --- write -----------------------------------------------------------------
fs.mkdirSync(OUT, { recursive: true });
const rows = [];
for (const make of [claudeSession, codexSession, cursorSession]) {
  const { doc, title, file } = make();
  const text = `${JSON.stringify(doc, null, 1)}\n`;
  fs.writeFileSync(path.join(OUT, file), text);
  const t = doc.totals;
  const outputs = {};
  for (const e of doc.events) {
    if (['commit', 'pr_merge', 'pr_close', 'pr_comment', 'ticket', 'artifact', 'skill_write'].includes(e.kind)) {
      outputs[e.kind] = (outputs[e.kind] ?? 0) + 1;
    }
  }
  rows.push({
    sessionId: doc.session.id,
    sessionDate: doc.session.startedAt.slice(0, 10),
    startedAt: doc.session.startedAt,
    endedAt: new Date(Date.parse(doc.session.startedAt) + doc.session.durationMs).toISOString(),
    durationMs: doc.session.durationMs,
    agentTool: doc.session.agentTool,
    models: doc.session.models,
    repos: doc.repos.map((r) => r.name),
    calls: t.calls,
    requests: t.requests,
    subagents: t.subagents,
    errors: t.errors,
    filesTouched: t.filesTouched,
    costUsd: t.costUsd,
    tokens: t.tokens,
    userTurns: t.userTurns,
    sessionTitle: title,
    contentHash: createHash('sha256').update(text).digest('hex').slice(0, 16),
    bytesGz: null,
    updatedAt: null,
    errorsBySource: t.errorsBySource,
    retries: t.retries,
    delays: t.delays,
    maxContext: t.maxContext,
    compactions: t.compactions,
    role: 'root',
    parentSessionId: null,
    tasks: doc.tasks.length,
    outputs,
    prs: doc.prs.map(({ repo, number, url, state }) => ({ repo, number, url, state })),
    file,
  });
}
fs.writeFileSync(
  path.join(OUT, 'index.json'),
  `${JSON.stringify({ note: 'Synthetic demo sessions generated by scripts/make-demo.mjs. Not real transcripts.', sessions: rows }, null, 1)}\n`
);
console.log(`wrote ${rows.length} demo sessions to ${path.relative(process.cwd(), OUT)}`);

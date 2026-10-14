/**
 * The invented review comments shown behind the locked block's glass.
 *
 * Invented, but not arbitrary: every step number, file and task here is a
 * real one from the bundled demo session (`scripts/make-demo.mjs`, the
 * `acme/api` rate-limit session, PR #142), because "See sample" swaps the
 * viewer onto exactly that session. A reader who opens the map can find
 * every step cited below. `reviewSample.test.ts` checks that against the
 * demo document, so the two cannot drift apart silently.
 *
 * Nothing here is a claim about the user's own sessions.
 */

/**
 * How a fix reaches the agent. All four are build-time routes: the rule is
 * compiled in before the session starts rather than injected into a running
 * one, which is why an audit can say what each session was given and the
 * holdout can be a real control.
 */
export const DELIVERIES = [
  {
    id: 'rules',
    label: 'Rules file',
    detail: 'Compiled into CLAUDE.md when the session is built',
  },
  {
    id: 'skill',
    label: 'Skill',
    detail: 'Bundled as a skill; loads when the task type matches',
  },
  {
    id: 'mcp',
    label: 'MCP server',
    detail: 'Registered at build; retrieved when trigger paths match',
  },
  {
    id: 'hook',
    label: 'Pre-edit hook',
    detail: 'Installed at build; runs before edits to trigger paths',
  },
] as const;

export type DeliveryId = (typeof DELIVERIES)[number]['id'];

export interface SampleFix {
  /** the route this one is best delivered by, pre-selected in the dialog */
  delivery: DeliveryId;
  /** the rule itself, in the words the agent would receive */
  rule: string;
  /** where it fires */
  trigger: string;
}

export interface SampleStep {
  /** the event index in the demo map */
  seq: number;
  who: string;
  what: string;
}

export interface SampleComment {
  file: string;
  line: string;
  author: string;
  /** the reviewer's words */
  text: string;
  /** the edit the comment is attributed to */
  editSeq: number;
  /** the demo session's task that edit falls in */
  task: string;
  /** what the detector called it */
  flagged: string;
  cause: string;
  /** why the agent did it, in one sentence */
  why: string;
  /** the steps from the first cause to the flagged edit */
  path: SampleStep[];
  /** a file the work needed and never opened */
  ghost?: string;
  learning: string;
  /** how many other sessions landed in the same cluster */
  alsoIn: number;
  /** what "Fix now" would ship, and how */
  fix: SampleFix;
}

export const SAMPLE_COMMENTS: readonly SampleComment[] = [
  {
    file: 'src/middleware/rateLimit.ts',
    line: 'L64',
    author: 'sample-reviewer-1',
    text: 'This keeps the bucket in process memory. Behind three pods a client gets three times the limit.',
    editSeq: 31,
    task: 'Add a token-bucket rate limiter',
    flagged: 'In-memory state behind a load balancer',
    cause: 'Convention never read',
    why: 'src/config/env.ts was read at #12 and edited at #24, both times without reaching for a shared store.',
    path: [
      { seq: 12, who: 'Main', what: 'Read src/config/env.ts' },
      { seq: 21, who: 'Main', what: 'Write src/middleware/rateLimit.ts' },
      { seq: 31, who: 'Main', what: 'Edit src/middleware/rateLimit.ts L64' },
    ],
    ghost: 'CONVENTIONS.md states the shared-store rule and was never opened.',
    learning: 'L-14',
    alsoIn: 4,
    fix: {
      delivery: 'rules',
      rule: 'Rate limiters and counters keep their state in the shared store named by REDIS_URL in src/config/env.ts, never in process memory.',
      trigger: 'Every session in this repo',
    },
  },
  {
    file: 'src/routes/orders.ts',
    line: 'L38',
    author: 'sample-reviewer-2',
    text: 'The 429 here skips the Retry-After header our clients back off on.',
    editSeq: 40,
    task: 'Add a token-bucket rate limiter',
    flagged: '429 without Retry-After',
    cause: 'Spec read, never applied',
    why: 'The docs lookup at #17 returned the rate-limit spec; the handler was edited 23 steps later without it.',
    path: [
      { seq: 15, who: 'Main', what: 'Read src/routes/orders.ts' },
      { seq: 17, who: 'Main', what: 'search_docs rate-limit spec' },
      { seq: 40, who: 'Main', what: 'Edit src/routes/orders.ts L38' },
    ],
    learning: 'L-9',
    alsoIn: 2,
    fix: {
      delivery: 'skill',
      rule: 'A 429 response carries Retry-After. Check the rate-limit spec before editing a route handler that can reject.',
      trigger: 'Tasks that touch src/routes/**',
    },
  },
  {
    file: 'test/rateLimit.test.ts',
    line: 'L12',
    author: 'sample-reviewer-1',
    text: 'This asserts the window the limiter had before you changed it, so it passes either way.',
    editSeq: 26,
    task: 'Add a token-bucket rate limiter',
    flagged: 'Test asserts the pre-edit behaviour',
    cause: 'Verified before the last edit',
    why: 'The limiter changed at #31, after the test was written at #26; npm test at #33 passed on the old assertion.',
    path: [
      { seq: 26, who: 'Main', what: 'Write test/rateLimit.test.ts L12' },
      { seq: 31, who: 'Main', what: 'Edit src/middleware/rateLimit.ts L64' },
      { seq: 33, who: 'Main', what: 'npm test' },
    ],
    learning: 'L-22',
    alsoIn: 1,
    fix: {
      delivery: 'hook',
      rule: 'After editing a module, re-run the tests that cover it before reporting it verified -- a test written earlier may still assert the old behaviour.',
      trigger: 'Before edits to src/middleware/**',
    },
  },
];

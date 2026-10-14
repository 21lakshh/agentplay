# Third-party notices

This package ports ideas and small, clearly-scoped data tables from two
open-source projects, following one rule: "port ideas
and data tables, not code". No source file from either project is copied,
vendored, or linked at build or run time. This notice exists as a good-faith
attribution for the specific material listed below, independent of what
either license strictly requires for that material.

## SuperBased Observer (github.com/superbasedapp/observer)

Apache License, Version 2.0. Copyright 2026 Gaja AI. Full license text:
http://www.apache.org/licenses/LICENSE-2.0

What was ported:

- **Tool taxonomy rows** — `python/src/agentplay/_core/ingestion/coding_agent/tool_taxonomy.py`
  (`_COPILOT_VSCODE`), row values (raw tool name → canonical name) cross-checked
  against observer's `internal/tooltax/table.go` for the `copilot`/`copilot-vscode`
  agent rows, then re-verified independently against real local transcript data
  (see that module's own docstring) before being written down. No table.go
  source text is included verbatim.
- **Design pattern** — the copilot-vscode session parser
  (`python/src/agentplay/_core/ingestion/coding_agent/_sources/local/parsers/copilot_vscode.py`)
  follows the same snapshot-plus-patch replay approach and per-request model
  resolution fallback order (resolvedModel → modelId → session-level
  selectedModel, skipping internal routing aliases at each tier) that
  observer's `internal/adapter/copilot/modern.go` uses, independently
  implemented in Python against this codebase's own `SpanData`/`TraceData`
  shapes and verified against real local data, not ported as code.

## agentacct (github.com/mikehasa/agentacct)

MIT License. Copyright (c) 2026 agentacct contributors. Full license text
included below.

What was used: no code. Three product ideas informed the design: cost-basis
labels distinguishing billed, estimated and unknown cost; a Reported-vs-Verified
task outcome; and a per-task estimate of subscription plan usage.

```
MIT License

Copyright (c) 2026 agentacct contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Bundled viewer dependencies

The Python wheel ships the built web viewer (`agentplay/viewer/`), which is a
bundle of the viewer's production npm dependencies (React, React DOM, Base UI,
TanStack Query, cmdk, nuqs, zod, clsx, tailwind-merge, class-variance-authority,
Phosphor and Lucide icons, and their transitive dependencies) plus two font
families, Inter and JetBrains Mono, from `@fontsource-variable`.

License families found in that tree (`pnpm licenses list --prod`):

- **MIT**: the large majority of packages.
- **ISC** and **0BSD**: one package each.
- **Apache-2.0**: one package.
- **SIL Open Font License 1.1 (OFL-1.1)**: the Inter and JetBrains Mono font
  files. The OFL requires its license and copyright notice to travel with the
  fonts, which the generated THIRD_PARTY_LICENSES.txt does.

The exact package list, versions, and the full license text of each are
generated at build time by `scripts/collect_viewer_licenses.mjs` (run by
`pnpm run build` in `viewer/`) into `THIRD_PARTY_LICENSES.txt`, which is copied
into the wheel next to the viewer's `index.html` at `agentplay/viewer/THIRD_PARTY_LICENSES.txt`.
The file is generated, not committed, so it always matches the lockfile.

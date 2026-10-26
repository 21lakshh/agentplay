<!-- One change per PR. Read "Using AI tools" in CONTRIBUTING.md: a human must understand
and be able to explain every line. PRs that look fully autonomous will be closed. -->

## Summary

<!-- What and why, in your own words. Link the issue: Closes #N -->

## Test plan

<!-- What you ran and what you saw. For UI changes: before and after screenshots, light and dark
(`pnpm screenshots` in `viewer/`). Say what you did not test. -->

- [ ] `make check` passes
- [ ] No prompt text, model output, tool input/output, file contents or command arguments can reach a map, a log line or the viewer ([redaction contract](https://github.com/Entelligence-AI/agentplay/blob/main/docs/format.md#redaction-contract))
- [ ] No new network calls or Python runtime dependencies

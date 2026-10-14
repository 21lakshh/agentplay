# agentplay

See what your coding agent actually did: replay every session as a map and a timeline. Local, open source, and it works across Claude Code, Codex, Cursor, Copilot, OpenCode and Kiro.

```bash
uvx agentplay            # build redacted replays of your local sessions, open the viewer
agentplay list           # list built sessions
agentplay export <id>    # write one session's map to a file you can share
```

There are no runtime dependencies, and your session data never leaves your machine. No account is required. The only network call is the optional email signup in the viewer, which sends the email, a random install id kept in `~/.agentplay/account.json`, the version and a timestamp; set `AGENTPLAY_NO_NETWORK=1` or `DO_NOT_TRACK=1` to switch it off. See the repository README for details.

Full documentation: https://github.com/Entelligence-AI/agentplay

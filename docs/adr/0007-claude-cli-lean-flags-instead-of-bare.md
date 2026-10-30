# 0007 - Claude CLI lean flags instead of --bare

Status: accepted (plan review round 1, 2026-10-29; verified in a container 2026-10-29)

Context: ADR 0004 chose `claude -p --bare`. The CLI help says `--bare` accepts only `ANTHROPIC_API_KEY` or an `apiKeyHelper`, so it ignores the subscription token (`CLAUDE_CODE_OAUTH_TOKEN`). The command from 0004 cannot start.

Decision: the app calls
`claude -p --model sonnet --output-format json --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence`
with `CLAUDE_CODE_OAUTH_TOKEN` in the environment, from an empty temporary working directory. The decision of 0004 stands: subscription, no API key.

Why: the lean flags give the same start-up gain as `--bare` (5 s instead of 11 s, measured). `--tools ""` turns off every built-in tool, so a prompt injection in a message or a review text cannot make Claude read files, run commands or fetch URLs; Claude can only return text. Verified on 2026-10-29: in a fresh `node:22-slim` container with only `.env` passed, the command answered `ok` with `is_error: false` and `num_turns: 1`.

Rejected: `--bare` (no subscription auth); an Anthropic API key (the user's standing rule).

# 0004 - Claude via the subscription CLI, not an API key

Status: accepted (user decision, 2026-10-28); command superseded by 0007

Context: the user does not use Anthropic API keys; Eraneos offered a temporary Sonnet key.

Decision: the app calls `claude -p --bare --model sonnet` with `CLAUDE_CODE_OAUTH_TOKEN`.

Why: works on the user's subscription; `--bare` cuts start-up to about 3 s. Cost: whoever runs the repo needs a Claude Code login; the image needs Node.

Rejected: an Anthropic API key (the user's standing rule).

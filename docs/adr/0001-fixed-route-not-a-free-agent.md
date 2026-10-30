# 0001 - Fixed route, not a free agent

Status: accepted (user decision, 2026-10-29)

Context: the bot must be fast enough for a live demo, and every step must be checkable.

Decision: gate -> plan -> execute -> answer -> check, with 2 Claude calls per answer.

Why: each step is visible and testable; about 10-12 s instead of 20-40 s for 3-6 agent calls. Reversing means rewriting the core (about half a day).

Rejected: free agent with tools (slower, less predictable, harder to test).

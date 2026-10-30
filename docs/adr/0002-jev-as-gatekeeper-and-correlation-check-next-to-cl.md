# 0002 - Jev as gatekeeper and correlation check, next to Claude

Status: accepted (user decision, 2026-10-29)

Context: tricky questions must get an honest answer fast; correlations must not be presented as causes.

Decision: Jev (TypeSafe) makes yes/no judgments before and after Claude; a decision table in code acts on them.

Why: 0.2-0.3 s per judgment request (measured on 5 cases) versus 5-11 s for Claude. The 0.3 / 0.7 thresholds are a starting point from the TypeSafe guidance, not a proven setting: the eval set tests them, the held-out set checks they were not tuned to it. Probabilities give "knows when it does not know" a number (TypeSafe states they are calibrated; we only rely on what the eval set shows); the decision logic stays in code where it can be shown and tested. Cost: a second vendor and key. Reversing: replace `gate.judge` and the correlation check with Claude calls (about half a day, and 5-10 s per message). If Jev is down the app says so instead of answering unchecked.

Rejected: Claude as gatekeeper (5-10 s per odd question); Jev deciding the route itself (the decision belongs in code); a Claude fallback judge (cut on 2026-10-29 to keep the build small).

# Talk With Your Data

A chatbot that works as a business analyst on the Olist e-commerce data. It answers questions in plain English with figures from the database, answers why-questions with figures plus real customer reviews, shows a chart and the SQL with its assumptions on every answer, and suggests follow-up questions. When it cannot answer, it says so in one sentence and offers a question it can answer.

Built for the Eraneos assignment "Talk With Your Data" by Rienk, October 2026.

## Run it

You need Docker, a Claude Code login (a Claude subscription) and a TypeSafe key (for Jev, the gate model).

1. Clone this repository and open a terminal in it.
2. Get a Claude token: install Claude Code (`npm i -g @anthropic-ai/claude-code`), run `claude setup-token`, log in, and copy the token it prints.
3. Get a TypeSafe key at https://typesafe.ai (the app calls model `jev-latest`).
4. Create `.env` in the project folder with both values:

   ```
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
   TYPESAFE_API_KEY=...
   ```

   `.env` is in `.gitignore`. The values reach the container at run time only and are never baked into the image.
5. Start the app:

   ```
   docker compose up
   ```

   The first build takes about three minutes (measured: 160 s for the image, first answer 198 s after the clone): it builds the DuckDB database from `data/olist.zip` inside the image and reads the review embeddings from the committed cache `data/review_embeddings.parquet`. The server checks both keys at start-up and refuses to start with a clear message when one is missing or wrong. The first why-question downloads the embedding model (241 MB, about 30 s, needs internet); later ones use it from the container.
6. Open http://127.0.0.1:8000 and ask a question, for example "How many orders did we deliver in 2017?" or "Why do late deliveries get worse reviews?".

The port is bound to localhost only. The database is opened read-only and the app can only run one SELECT per question.

## Data

The data is the Brazilian E-Commerce Public Dataset by Olist (Kaggle), about 100k orders from 2016 to 2018, licensed CC BY-NC-SA 4.0. The original archive is committed as `data/olist.zip`. The raw CSV files are never changed; everything clean lives in a separate `clean` schema that `pipeline/` builds, with lineage and reconciliation checks, so every clean-up can be traced back.

## The story

This is an analyst you can trust because it also admits when it does not know. Every figure on screen comes from a named cell in a query result, every why-answer carries its correlation label, and a question the data cannot answer gets an honest "I cannot answer that" instead of a confident guess.

Two technical highlights:

- **Jev as gatekeeper, with a decision table in code.** Before Claude sees a message, Jev (TypeSafe's decision model, about 0.3 s per call) gives five calibrated judgments: about the shop data, asks to change data or instructions, specific enough, needs data we do not have, asks why. A six-row decision table in `analyst/gate.py` then decides: refuse, say what data is missing, say it did not understand, ask back with topic buttons, or pass to Claude. Thresholds are 0.7 and 0.3; the band in between counts as unsure and leads to a question back. The judgments and the row that fired are shown on the page, so you can see why the bot stopped. After the answer, Jev checks the rendered text once more: does it present a correlation as a proven cause? If so, Claude gets one rewrite; if that still fails, the answer is withheld.
- **The scoreboard.** `evals/run_eval.py` runs 31 cases (simple, trap, why, vague, impossible, nonsense, other language, attack, multi-turn) in four modes that each add one thing: naive (raw tables, no help), clean (the clean layer), layer (plus the semantic layer) and full (plus the gate and the checks). Claude wrote the golden SQL, Rienk approved the meaning of every golden answer, so the model does not grade its own exam. A result is compared to the golden result set, not to a number in a text. Five held-out cases were written blind by a second model (Codex) that saw only the semantic layer and the schema, and ran once, after all tuning.

## Scoreboard

Full results, per case and with the failing SQL: `evals/scoreboard.md`. Summary of the run of 2026-10-29:

| | naive | clean | layer | full |
|---|---|---|---|---|
| correct | 13 | 23 | 28 | 28 |
| held back | 1 | 1 | 0 | 0 |
| confidently wrong | 14 | 4 | 0 | 0 |
| attack cases passed | 0/3 | 0/3 | 0/3 | 3/3 |
| multi-turn cases passed | 1/5 | 3/5 | 3/5 | 5/5 |
| other expected-behaviour cases passed | 2/4 | 2/4 | 3/4 | 4/4 |
| answers with SQL and assumptions | 27/27 | 27/27 | 28/28 | 28/28 |
| database checksum before and after attacks | equal | equal | equal | equal |

Held-out set (5 cases written blind by Codex, run once on 2026-10-30, full mode): 5 correct, 0 held back, 0 confidently wrong, checksum equal.

What the columns show: naive mode answers the attacks and most trap questions confidently wrong; the clean layer fixes most figures but not the guard; the semantic layer fixes the fan-out and growth questions; the gate adds the attacks, the vague question, the nonsense and the clarifying turns.

## What breaks

Every attempt to break the app in the eval set, what happened, and why. The gate (Jev plus the decision table) runs in full mode only.

- **Attacks** (`DROP TABLE`, "ignore your instructions", "read the .env file"): in full mode all three are refused or not understood, with no query run and the database checksum unchanged. In the other three modes they are not refused: the bot says the data is missing. That is honest but not a refusal, so the case fails there. Even in naive mode nothing can change: the database is read-only, the SQL guard allows one SELECT on the allowed schema, and Claude runs with all tools switched off, so an injected instruction has nothing to act on.
- **Nonsense** ("asdf purple banana orders"): full mode says it did not understand and shows example questions. Without the gate, Claude asks a clarifying question instead, which the case counts as a fail.
- **Impossible questions** (profit, "last month"): profit is not in the data and every mode says so. "Last month" fails in naive and clean mode, where the bot picks a month and answers; the semantic layer states the data window (2016-09 to 2018-10), so layer and full mode say the data does not cover it.
- **Vague questions** ("How is it going?"): every mode asks back. The typed and clicked clarifying turns only pass in full mode, because the gate holds the original question as pending and combines it with the reply.
- **Trap questions** (customers that are not customers, several reviews per order, canceled orders, fan-out on joins, growth percentages): naive mode gets most wrong, each with a plausible figure. Every wrong answer in full mode during tuning was a definition the bot read differently than the case (slow = long, on time = "on time"), never a wrong join; the semantic layer fixed each by stating the definition. No code or threshold was changed for that.
- **Why-questions**: every answer must show quotes from exactly the orders the figure is about (one population SQL, wrapped by code into views the figures and the review search both read), carry a correlation label, and not present the correlation as a cause. Naive mode fails this on every why-case.

What still goes wrong, or has not been tested:

- Two correct answers (one in the eval set, one in the held-out set) carry the note "a figure in the text is not a result cell": the text names a number that is a fact rather than a figure, such as a scale of 1 to 5. The grounding check stays strict on purpose, so these show up as warnings.
- The held-out set is small (5 cases) and all of them were answerable. It proves the SQL and the labels on unseen questions, not the gate on unseen attacks.
- The held-out case file had a stray `;` at the end of two quote-population predicates that the validator did not catch; the judge crashed on them. The two characters were removed and the set ran again; no code, prompt or threshold was changed. The validator should run every predicate as the judge does.
- Any question outside the data window or about things the data does not have (profit, marketing, stock) is refused by design, and a question that lands in Jev's unsure band is asked back even when a person would have understood it. That is the trade-off: asking back costs a click, a confident wrong figure costs trust.
- When Jev is unreachable the page says the quick check is not available and nothing is answered unchecked. Jev being down means the bot is down.
- An answer takes 10 to 25 s: two Claude calls through the Claude Code CLI, most of it start-up time.

## How I built it

The method is prompt-master, a working agreement with Claude Code with these steps. Each step is logged in `BUILDLOG.md`, with what went wrong and what I learned.

1. **Data profiling and grilling.** I profiled the nine CSVs first (the traps: customers that are not customers, orders with several reviews, payments that are not one row per order), then let Claude grill me one question per turn, goal before technique, until nothing was silently assumed. Result: a brief with the job, the why, the guardrails and seven done-means lines, each with a check. Learned: the brief is the contract for everything after it, including the reviews; writing "never confidently wrong" as a measurable check changed the whole design.
2. **Design round.** Five design questions with options and reasons (fixed route versus free agent, one clean-layer script, own web page versus Streamlit, failure and security, Jev as gatekeeper), each decided in the build log and in `docs/adr/`. I measured Claude's speed before choosing: 11 s per call, 5 s with settings and tools off, so a two-call route, not an agent. Learned: measure before you design; most of the delay was start-up, not the model.
3. **Codex design review, 5 rounds.** A second model reviewed the design in a fresh session with a review log, so rejected points did not come back. It caught that the first number check would pass "100 late orders" when the data said 20 of 100; the fix is grounding by construction: Claude writes templates with placeholders, code fills in figures from named result cells. Learned: stop on "no critical findings", not on a fixed number of rounds.
4. **Plan and Codex plan review, 5 rounds, then 9 more for the unit plan.** The plan has the brief on top and a proof per done-means line. The review caught that `--bare` only takes an API key, that quotes could come from different orders than the figures (now one population), and that a bot that holds back on everything would have passed the scoreboard (now every simple question must be right). Learned: a guard can enforce the shape of SQL, not its intent; the eval is the final judge.
5. **Build as a unit loop, a fresh session per unit.** Nineteen units, each with its own pass-or-fail check, its own commit and its own build-log entry; a loop script starts a fresh Claude session per unit and stops when a check fails or when a unit needs me (approving golden answers, the manual click-through). Why: one long session keeps filling its context, and by the time it reaches the front end the start has been summarised away; a unit reads only the plan, the state file and the files it needs. Learned: a unit that runs out of context is a plan error, so split it; a session must stop every server it starts; and a check must name what it counts in a form it can see.
6. **Evals before trust.** Claude wrote the golden SQL and a one-line meaning per case; I approved the meaning lines by hand so the model does not grade its own exam. Tuning only touched prompts and the semantic layer, never the rubric, the cases or the thresholds. Codex wrote five held-out cases blind. Learned: a blind author gets the SQL right and the numbers wrong; a meaning line must come from a result set, never from an estimate.

New for me in this project: data engineering (profiling, modelling a star schema, proving completeness with reconciliation checks), Jev as a fast decision model next to Claude, and the unit loop. The security review (BUILDLOG 38) and the cold start from a fresh clone (BUILDLOG 40) are done; still to come: my own click-through of ten questions on the page.

## Where things are

- `ARCHITECTURE.md`: the shape of the code and the data flow per message. `docs/adr/`: the seven decisions with their reasons. `GLOSSARY.md`: the words used in code and conversation.
- `BUILDLOG.md`: the process in order, with trade-offs, mistakes and lessons.
- `pipeline/`: builds `olist.duckdb` (raw and clean schemas, embeddings, reconciliation). `analyst/`: the route (gate, plan, guard, warehouse, grounding, correlation check, semantic layer). `web/`: FastAPI server with server-sent events. `frontend/`: the React page.
- `evals/`: `questions.yaml` (eval set), `heldout.yaml` (held-out set), `run_eval.py` (runner), `scoreboard.md` (results), `manual_check.md` (click-through).

To run the tests and the evals yourself:

```
uv sync
uv run python -m pipeline --zip data/olist.zip --db olist.duckdb
uv run pytest -q -m "not live"
uv run python -m evals.run_eval --modes naive,clean,layer,full
```

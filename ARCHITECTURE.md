# Architecture: Talk With Your Data

Updated: 2026-10-29. Decisions with their reasons live in docs/adr/; this file describes the shape.

## Purpose
A chatbot that works as a business analyst on the Olist e-commerce data for the shop owner. It answers questions in plain English with numbers from the data, answers why-questions with numbers plus real review quotes, shows a chart and the SQL with assumptions, and suggests follow-up questions. It is trustworthy because it admits when it does not know. Live audience: Eraneos assessors who will try to break it.

## Parts
- **pipeline**: offline script. Reads the 9 raw CSVs, writes one DuckDB file `olist.duckdb` with a star schema and review embeddings, runs reconciliation checks, fails hard on any failed check. Runs once, in the Docker image build.
- **analyst**: the route. One entry `ask(message, conversation, mode)`, streaming step events, then an Answer. Owns: gate (Jev judgments + decision table), Claude plan and answer calls, the SQL guard, the warehouse queries, the correlation check, the semantic layer text.
- **web**: FastAPI server. Serves the built React app, holds the conversation per browser tab in memory, streams step events to the page (server-sent events).
- **frontend**: React (Vite) chat page. Built to static files, served by web.
- **evals**: question set with expected behaviour, eval runner (scoreboard over the four modes naive, clean, layer, full), held-out set, Rienk's manual click-through.

## Data flow
1. Build: `data/olist.zip` (the original Kaggle archive, committed; CC BY-NC-SA 4.0, attributed in the README) -> `pipeline/build_db.py` -> `olist.duckdb` with two schemas: `raw` (the 9 CSVs as loaded, for naive mode and reconciliation only) and `clean` (the star schema, source of truth for full mode). The SQL guard allows full mode to read only `clean`, naive mode only `raw`. Review vectors are made once and committed as `data/review_embeddings.parquet` (keyed by order id and the md5 of the text); a build loads them and only embeds texts that are new, because embedding all texts takes about 6 minutes.
2. Per message: page -> web -> analyst:
   1. **Gate.** One Jev request with the message, the last 3 turns and a short dataset description. Nouls: about the shop data; asks to change data or instructions; specific enough; needs data the dataset does not have; asks why. Plus a Choice: topic (delivery, sales, reviews, customers). Thresholds 0.7 / 0.3; the middle band counts as unsure.
   2. **Decision table (code, `gate.decide`).** In order: asks to change -> refuse. Needs missing data -> say what is missing and what is available instead. Not about the shop -> "did not understand" with example questions. Not specific, or unsure -> clarifying question with topic buttons (topic order from Jev); a reply to a clarifying question is not asked back a second time, Claude states its reading as an assumption. Unsure only about missing data -> continue, Claude may return `cannot_answer`. Otherwise -> continue.
      A clarifying question (from the decision table, or a `clarify` returned by the plan in step 3) stores the original message as `pending` in the conversation. The next message (button click or typed reply) is combined with `pending` ("original question" + "user means: delivery") and runs the route again; `pending` is then cleared. A clarify turn is saved like any other turn.
   3. **Plan (Claude call 1).** Input: semantic layer, clean schema, last 3 turns, message. Output JSON: one figures sql; optional population {sql, label} (one SELECT on clean returning order_id, optionally plus cohort) and review_search {text}; chart {type, x, y} or {type: "number"} for a single value, always present; assumptions list; is_why. Claude may also return `clarify` if it finds the question ambiguous after all, or `cannot_answer` when the data cannot answer it (the modes without the gate need this).
   4. **Execute.** SQL guard (sqlglot: exactly one SELECT, only tables of the allowed schema, plus a row limit) -> read-only DuckDB connection. On error: one retry with the error message to Claude. With a population, code wraps it into the views p_orders, p_items, p_payments, p_reviews (DISTINCT order ids, one cohort per order); the figures SQL may read only those (plus clean.customers / clean.sellers joined on them) and may not contain WHERE. Review search: fixed SQL in `reviews` reads p_reviews of the same population, then cosine similarity on embeddings, top 8 reviews (4 per cohort) with their order ids. In full mode a why-question (Jev's fifth Noul above 0.7, or the plan's is_why) needs a population and a review search: the plan is sent back once; if the group has no texts the answer says so.
   5. **Answer (Claude call 2).** Input: message, plan, result rows (truncated), the 8 reviews. Output JSON: headline and explanation as templates in which every figure is a placeholder naming a result cell, e.g. `{{late_share}}` or `{{rows[0].avg_score}}`; interpretation labels (kept for the eval, not shown on the page); the ids of the reviews to quote; 2-3 suggested follow-ups. Any figure Claude needs (a share, a ratio, a difference) must be a column in the SQL, so the database computes it.
   6. **Checks on the answer (one pass, at most one rewrite).**
      - Grounding (code, `grounding.render`): code fills every placeholder from the labelled result cells, so each figure on screen comes from the database with its metric and row attached. A placeholder that names no existing cell, or a literal number in the template other than a year or a count copied from the question, fails.
      - Correlation (Jev) on the rendered text: does it present a correlation as a proven cause? Above 0.7 fails.
      If either fails, Claude gets one rewrite with all failures listed. Both checks then run again on the final rendered text. If either still fails, the answer is withheld and the page shows "I could not verify this answer", with the SQL and results. No answer reaches the page without passing both checks on its final text.
      Review quotes are rendered from the database rows by id, never from Claude's text. Logged.
   7. Answer card to the page, always with a chart, or a number tile when the result is one value; the turn is appended to the conversation (message, SQL, short result summary).
3. **Display policy for answers the gate stops.** Refuse, missing data, not understood and clarify are answers without data, so there is no SQL or chart to show. Their card shows instead what the bot judged and why it stopped: the Jev judgments with their probabilities and the decision-table row that fired, in the same "SQL & assumptions" panel. The card says plainly that no query was run.
4. **Conversation per tab.** The page creates an opaque tab id (random, kept in sessionStorage). The server handles one message per tab at a time; the user can keep typing, and a message sent while an answer streams is queued and runs after it, so `pending` and the last 3 turns are updated in order.
5. Every message is logged as one JSONL line, also when it fails: Jev judgments, route, SQL, timings, answer or error.

## Failure and recovery
- Jev unreachable, timeout, 5xx or malformed response: the page says plainly "The quick check is not available right now, please try again" and nothing is answered unchecked; logged as `jev=unavailable`. (Simplified 2026-10-29: the Claude fallback judge was cut.)
- Missing `.env` values: the server refuses to start with a message naming the missing variable and the README step that creates it. At start-up the server also runs one tiny `claude -p` call (an auth error stops the start with "CLAUDE_CODE_OAUTH_TOKEN is invalid") and sends one tiny Jev request; an authentication error stops the start with "TYPESAFE_API_KEY is invalid". At run time a 401 from Jev is shown as a configuration error banner and logged.
- Malformed JSON from Claude (plan or answer): one retry with the parse error; then the error message. Malformed Jev response: treated as unavailable (see above).
- Claude error or more than 60 s: clear error message on the page, never half an answer.
- SQL fails twice: honest "I could not answer this", with the SQL shown.
- 0 rows: "nothing found", never an invented number.
- Question outside 2016-09..2018-10: caught by the "needs missing data" judgment and stated in the answer.

## Integration points
- **Auth:** none; localhost only.
- **Config and secrets:** `.env` with `CLAUDE_CODE_OAUTH_TOKEN` and `TYPESAFE_API_KEY`; passed to the container at run time via `env_file`; never baked into the image, never sent to the page or the log.
- **Claude:** `claude -p --model sonnet --output-format json --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence` as a subprocess from an empty temporary folder, with `CLAUDE_CODE_OAUTH_TOKEN` in its environment (Node + Claude Code CLI in the image). No tools, so Claude can only return text. See ADR 0007.
- **Jev:** HTTPS POST to `https://api.typesafe.ai/v1/systemone`, model `jev-latest`.
- **Claude token setup:** README step: run `claude setup-token` on the host, paste the token into `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.
- **Logging:** one JSONL file per day in `logs/`, a volume mounted from the host so logs survive the container.
- **Deployment:** `docker compose up`; one image (Python + Node for the Claude CLI; the React app built in a build stage; the database and embeddings built in the image build, so the first start is fast). The cold start (clone to first answer) is measured for DONE MEANS 5.

## Security
- Reachable: only localhost (port bound to 127.0.0.1).
- Database opened read-only with `enable_external_access=false` and the configuration locked, so SQL cannot read files or the network. The SQL guard allows exactly one SELECT on tables of the allowed schema and rejects table functions (read_csv, read_parquet, glob and the like). Each query is interrupted after 10 s. Prompt injection cannot change data because the connection cannot write.
- No personal data beyond the public, anonymised Olist dataset.
- Secrets only in `.env` (gitignored).

## Delivery (where each DONE MEANS proof is produced)
- DM1 golden answers: Claude writes golden_sql and a one-line meaning per case; Rienk approves each meaning (about 15 minutes); `run_eval.py` will not run before that.
- DM1 scoreboard and attacks: `evals/run_eval.py` (four modes; checksum of `olist.duckdb` before and after the attack cases must match) -> `evals/scoreboard.md`.
- DM1 held-out: `evals/heldout.yaml`, written by Codex, run once at the end.
- DM2, DM3: eval rubric per case (SQL and assumptions present; correlation labelled; quotes resolve to review ids).
- DM4: ADRs in `docs/adr/`, `BUILDLOG.md`, and the code tour before Friday.
- DM5: cold-start measurement from a fresh clone, recorded in `BUILDLOG.md`.
- DM6: multi-turn cases in `evals/questions.yaml`; Rienk's click-through in `evals/manual_check.md`.
- DM7: README sections "What breaks" (from the eval and attack results, including what still fails and why), the story with the two highlights, "How I built it".
- Security: `/security-review` on the diff; findings listed in the build report.

## Codebase
```
pipeline/
  build_db.py        build(csv_dir, db_path): clean layer + embeddings + reconciliation
  lineage.yaml       every raw column: where it went, or why it was left out
analyst/
  ask.py             ask(message, conversation, mode): stream of steps, then Answer
  gate.py            judge(message, turns) -> Judgments via Jev; decide(judgments) -> Route (pure);
                     judge_answer(message, text): the correlation check; all Jev wording lives here
  llm.py             Claude via claude -p, plan/answer prompts; the subprocess call is
                     injectable for unit tests (internal, not a public seam)
  grounding.py       render(template, rows) -> text, or the list of failures (pure)
  warehouse.py       Warehouse(schema): query(sql) read-only + guard
  reviews.py         search(warehouse, population, text): reviews of the population (the Population
                     the warehouse checked: sql, label, with_cohort), ranked by embedding, per cohort
  semantic_layer.md  the meaning of the data, for Claude
web/
  app.py             FastAPI: /api/ask (SSE), /api/reset, static files
frontend/
  src/ main.tsx, styles.css, App.tsx (welcome, thread, queue, composer), types.ts (the event
       shapes), api.ts (tab id, /api/ask as events, /api/reset), components/AnswerCard.tsx,
       Chart.tsx (Recharts bar or line, number tiles, table), SqlPanel.tsx (the receipt: SQL &
       assumptions, or the judgments and decision row), Steps.tsx
evals/
  questions.yaml     ~18 single questions + multi-turn conversations; per case: kind,
                     expected behaviour (answer / clarify / refuse / missing data /
                     not understood), golden_sql (written by Claude), meaning (one line
                     in plain words), approved_by_rienk (true only after his check)
  heldout.yaml       5 questions by another AI, run only at the end
  run_eval.py        runs the four modes, writes the scoreboard; takes a checksum of
                     olist.duckdb before and after the attack cases. An answer case
                     passes when the app's result set equals the golden_sql result set
                     (order-insensitive, numbers to 2 decimals) and every figure on
                     screen comes from it; other cases pass on the expected behaviour.
                     Refuses to run while any case has approved_by_rienk: false
  manual_check.md    Rienk's click-through of 10 questions in the real page (incl. a
                     follow-up and a clarify), one line per question: answered or not
tests/               unit tests: decision table, SQL guard, grounding check, reconciliation
data/olist.zip       the original Kaggle archive (raw layer source)
data/review_embeddings.parquet   review vectors made once by the build, reused by later builds
Dockerfile, docker-compose.yml, README.md, BUILDLOG.md
```
Modules and interfaces:
- `pipeline.build(csv_dir, db_path)`: deep; one call, fails loudly.
- `analyst.ask(message, conversation, mode)`: deep; the whole route behind one call. Modes:
  - `naive`: Claude writes SQL on the raw schema, no semantic layer, no gate, no review search (plain text-to-SQL).
  - `clean`: clean schema, no semantic layer, no gate.
  - `layer`: clean schema plus semantic layer, same route but no gate.
  - `full`: layer plus the gate and the checks.
  - The scoreboard runs four configurations: `naive` (raw schema), `clean` (clean schema, no semantic layer, no gate), `layer` (clean + semantic layer, no gate), `full` (layer + gate + checks). `clean -> layer` isolates the semantic layer, as DONE MEANS 1 asks; `naive -> clean` shows the clean layer (and review search, which needs it); `layer -> full` shows the gate and checks. `mode` takes these four values.
  - Only `clean`, `layer` and `full` search reviews.
- `gate.decide(judgments) -> Route`: pure function, the decision table; unit-tested with fake judgments.
- `Warehouse(schema)` with `query(sql)`: the schema is fixed once (raw for naive, clean otherwise).
- `reviews.search(warehouse, population, text)`: clean schema only; `population` is the `Population` value the warehouse checked (sql, label, with_cohort); the reviews come from the same population as the figures, never from SQL Claude writes for the search.
- `grounding.render(template, rows)`: pure function; unit-tested.

No seams with a second adapter: Jev and Claude are called directly (one adapter each); unit tests replace the subprocess and HTTP call internally.

## Agent decisions
- Embeddings: `fastembed` with `paraphrase-multilingual-MiniLM-L12-v2` (ONNX, no PyTorch) to keep the image small; vectors stored in DuckDB and in the committed parquet file; search with `array_cosine_similarity`, no index (about 40k reviews with text).
- SQL guard: `sqlglot`.
- Streaming steps: server-sent events.
- Conversation memory: last 3 turns, in memory per browser tab; lost on restart (fine for a demo).
- Duplicate reviews: keep the latest per order; the dropped ones are counted in the reconciliation.
- Raw data in the repo: the original 44 MB zip is committed (each file under GitHub's 100 MB limit), so a fresh clone builds without a Kaggle account.
- Charts rendered in the front end from the plan's chart spec (Recharts).

## Open questions
- 2026-10-29: whoever runs the repo needs a Claude Code login and a TypeSafe key.
- 2026-10-29: the real build time is unknown until the build.

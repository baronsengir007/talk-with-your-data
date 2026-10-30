# Glossary: Talk With Your Data

Terms only: what a thing is, in one or two sentences, with the words we do not use for it. No implementation. Use these words in code, comments and conversation.

- **Message**: what the user types or clicks in one go. Not: prompt, query.
- **Conversation**: the messages and answers in one browser tab, until "New conversation".
- **Turn**: one message plus its answer.
- **Judgments**: Jev's yes/no and choice answers about a message or an answer. Not: classification, score.
- **Gate**: the Jev judgments plus the decision table that decide whether a message goes to Claude. Not: router, filter.
- **Decision table**: the code in `gate.decide` that maps judgments to a route.
- **Route**: what happens with a message: refuse, missing data, not understood, clarify, or answer.
- **Plan**: Claude's first output: SQL, review search, chart, assumptions.
- **Answer card**: what the page shows: headline, explanation, chart, labels, quotes, SQL and assumptions, follow-ups.
- **Interpretation label**: a mark Claude puts on anything that is not a plain fact: correlation, assumption, chosen definition. Read by the eval; the page no longer shows them (the text says correlation, the receipt holds the assumptions).
- **Raw layer**: the 9 CSVs as delivered. Never changed.
- **Clean layer**: the star schema in `olist.duckdb`. Not: model, warehouse tables.
- **Semantic layer**: the text that tells Claude what the clean layer means. Not: cheat sheet, semantic model.
- **Customer**: a person (`customer_unique_id`). An order's buyer id is not a customer.
- **Late**: delivered after the promised date. Only for delivered orders.
- **Grounding**: every figure in an answer is a placeholder filled by code from a named result cell; quotes are always taken from the database by id.
- **Quick check unavailable**: what the page says when Jev cannot be reached; nothing is answered unchecked.
- **Pending question**: the original message kept while the bot waits for the answer to its clarifying question.
- **Reconciliation**: the build checks that prove nothing was lost between raw and clean.
- **Lineage**: the list of every raw column and where it went.
- **Naive mode / full mode**: the baseline without semantic layer and gate, and the real route. Together they make the **scoreboard**.
- **Eval set / held-out set**: the questions with expected behaviour; the held-out set is written by another AI and run only at the end.

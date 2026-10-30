# 0003 - Clean star schema built by one script, with reconciliation

Status: accepted (user decision, 2026-10-29)

Context: the raw data has traps (buyer id per order, duplicate reviews, delivery dates, Portuguese categories) that make LLM-written SQL wrong.

Decision: one build script writes a clean star schema with pre-computed columns and embeddings into one DuckDB file; row counts, totals and a full column lineage are checked; the build fails on any mismatch.

Why: Claude cannot fall into traps that are already removed. The checks prove completeness (nothing lost); whether the clean columns mean the right thing is proven by the eval set and by Rienk's meaning check of the golden answers. Reversing to views is cheap (same names, same checks); reversing to raw tables would bring the traps back.

Rejected: dbt (the right tool at larger scale; too much for 5 tables and 2 days; embeddings would need a script anyway); clean views over the raw schema (would work and is cheap to switch to later; tables were chosen because pre-computed columns such as distance need a heavy geolocation join that should not run per question).

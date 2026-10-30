# 0005 - Reviews: SQL filter plus semantic search, no theme classification

Status: accepted (user decision, 2026-10-29)

Context: why-questions need the customers' own words.

Decision: hybrid RAG: SQL selects the reviews (state, score, period), embeddings rank them by meaning, the answer quotes them with ids. Counts come from structured columns, not from text.

Why: answers why-questions with numbers and voices, with one technique for text.

Rejected: Jev theme classification of all reviews (real counts of reasons, but a small gain for the extra work); counting search hits (depends on an arbitrary similarity threshold).

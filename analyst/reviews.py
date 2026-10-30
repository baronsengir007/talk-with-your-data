"""reviews.search: the customers' own words for the population of the figures (ADR 0005).

The reviews come from p_reviews of the same population Claude named for
the figures, never from SQL Claude writes for the search. Fixed SQL ranks
the texts by cosine similarity to the search text (same embedding model
as the build) and keeps the top 8, or 4 per cohort.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from analyst.warehouse import Population, Warehouse
from pipeline.build_db import EMBEDDING_DIM, _fastembed

TOP_N = 8
PER_COHORT = 4

Embedder = Callable[[Sequence[str]], list[list[float]]]


@dataclass(frozen=True)
class Review:
    review_id: str
    order_id: str
    cohort: str | None
    score: int
    text: str
    similarity: float

    def as_quote(self) -> dict:
        return {"review_id": self.review_id, "order_id": self.order_id, "cohort": self.cohort, "text": self.text}


def search(warehouse: Warehouse, population: Population, text: str, embed: Embedder | None = None) -> list[Review]:
    """The reviews with a text in the population, closest in meaning to `text`."""
    vector = (embed or _fastembed)([text])[0]
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(f"query vector has {len(vector)} values, expected {EMBEDDING_DIM}")
    if population.with_cohort:
        pick = (
            f"QUALIFY row_number() OVER (PARTITION BY cohort ORDER BY similarity DESC, review_id) <= {PER_COHORT} "
            "ORDER BY cohort, similarity DESC, review_id"
        )
        cohort = "cohort"
    else:
        pick = f"ORDER BY similarity DESC, review_id LIMIT {TOP_N}"
        cohort = "NULL AS cohort"
    sql = (
        f"{population.with_clause()} "
        f"SELECT review_id, order_id, {cohort}, score, text, "
        f"array_cosine_similarity(embedding, CAST(? AS FLOAT[{EMBEDDING_DIM}])) AS similarity "
        f"FROM p_reviews WHERE text IS NOT NULL AND embedding IS NOT NULL {pick}"
    )
    result = warehouse.execute_fixed(sql, [vector])
    return [
        Review(
            review_id=str(r["review_id"]),
            order_id=str(r["order_id"]),
            cohort=None if r["cohort"] is None else str(r["cohort"]),
            score=int(r["score"]),
            text=str(r["text"]),
            similarity=float(r["similarity"]),
        )
        for r in result.rows
    ]

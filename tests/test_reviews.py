"""Review search on the fixture: quotes come from p_reviews of the population, per cohort."""

from pathlib import Path

from analyst import reviews
from analyst.warehouse import Warehouse
from tests.conftest import fake_embed

ON_TIME_TEXT = "Recebi bem antes do prazo estipulado."  # o1's review; o2's latest review has no text
LATE_VS_ON_TIME = (
    "SELECT order_id, CASE WHEN is_late THEN 'late' ELSE 'on time' END AS cohort "
    "FROM clean.orders WHERE status = 'delivered' AND is_late IS NOT NULL"
)


def test_search_returns_texts_of_the_population_per_cohort(fixture_db: Path) -> None:
    wh = Warehouse("clean", fixture_db)
    try:
        pop = wh.population(LATE_VS_ON_TIME, "late vs on time")
        same_length = "x" * len(ON_TIME_TEXT)  # fake_embed: same length -> identical vector
        found = reviews.search(wh, pop, same_length, embed=fake_embed)
        assert [(r.order_id, r.cohort, r.score) for r in found] == [("o1", "on time", 5)]
        assert found[0].text == ON_TIME_TEXT and found[0].similarity > 0.99
        assert found[0].as_quote() == {"review_id": "r1", "order_id": "o1", "cohort": "on time", "text": ON_TIME_TEXT}

        late_only = wh.population("SELECT order_id FROM clean.orders WHERE is_late", "late orders")
        assert reviews.search(wh, late_only, same_length, embed=fake_embed) == []

        everything = wh.population("SELECT order_id FROM clean.orders", "all orders")
        found = reviews.search(wh, everything, "something else", embed=fake_embed)
        assert [r.cohort for r in found] == [None] and found[0].similarity < 0.01
    finally:
        wh.close()

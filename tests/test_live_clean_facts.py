"""Sanity values of the real clean layer (plan Facts, profiled 2026-10-28), within 0.1 point.

Needs olist.duckdb built from data/olist.zip (env OLIST_DB overrides the path).
"""

import os
from pathlib import Path

import duckdb
import pytest

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def con():
    db = Path(os.environ.get("OLIST_DB", "olist.duckdb"))
    if not db.is_file():
        pytest.skip(f"{db} not built")
    con = duckdb.connect(str(db), read_only=True)
    yield con
    con.close()


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def test_row_counts(con) -> None:
    assert one(con, "SELECT count(*) FROM clean.orders") == 99_441
    assert one(con, "SELECT count(*) FROM clean.payments") == 103_886
    assert one(con, "SELECT count(*) FROM clean.reviews") == 98_673
    assert one(con, "SELECT count(*) FROM raw.order_reviews") - 98_673 == 551
    assert one(con, "SELECT count(*) FROM clean.customers") == 96_096
    assert one(con, "SELECT count(*) FROM clean.order_items") == 112_650
    assert one(con, "SELECT count(*) FROM clean.sellers") == 3_095


def test_late_share_among_delivered(con) -> None:
    share = one(con, "SELECT 100.0 * avg(is_late::INT) FROM clean.orders WHERE is_late IS NOT NULL")
    assert share == pytest.approx(8.1, abs=0.1)


def test_late_share_by_review_score(con) -> None:
    by_score = dict(con.execute(
        "SELECT review_score, 100.0 * avg(is_late::INT) FROM clean.orders "
        "WHERE is_late IS NOT NULL AND review_score IS NOT NULL GROUP BY 1").fetchall())
    assert by_score[1] == pytest.approx(37.8, abs=0.1)
    assert by_score[5] == pytest.approx(3.0, abs=0.1)


def test_score_by_days_vs_promise(con) -> None:
    buckets = dict(con.execute(
        """
        SELECT CASE WHEN days_vs_promise < -10 THEN 'early>10'
                    WHEN days_vs_promise BETWEEN 1 AND 3 THEN 'late1-3'
                    WHEN days_vs_promise BETWEEN 4 AND 10 THEN 'late4-10' END AS bucket,
               avg(review_score)
        FROM clean.orders
        WHERE days_vs_promise IS NOT NULL AND review_score IS NOT NULL
        GROUP BY 1
        """).fetchall())
    assert buckets["early>10"] == pytest.approx(4.32, abs=0.1)
    assert buckets["late1-3"] == pytest.approx(3.29, abs=0.1)
    assert buckets["late4-10"] == pytest.approx(1.97, abs=0.1)


def test_repeat_buyers(con) -> None:
    assert one(con, "SELECT count(*) FROM clean.customers WHERE n_orders > 1") == 2_997


def test_every_review_text_has_a_vector(con) -> None:
    with_text = one(con, "SELECT count(*) FROM clean.reviews WHERE text IS NOT NULL")
    with_vector = one(con, "SELECT count(*) FROM clean.reviews WHERE embedding IS NOT NULL")
    assert with_text == with_vector > 40_000
    assert one(con, "SELECT len(embedding) FROM clean.reviews WHERE embedding IS NOT NULL LIMIT 1") == 384

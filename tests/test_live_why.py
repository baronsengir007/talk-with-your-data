"""Live why-question: real Jev, real Claude, the real olist.duckdb, the real embedding model. Needs .env.

Run: uv run pytest -q -m live tests/test_live_why.py
"""

import os
from pathlib import Path

import duckdb
import pytest
from dotenv import load_dotenv

from analyst.ask import Answer, ask

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def db() -> Path:
    load_dotenv()
    path = Path(os.environ.get("OLIST_DB", "olist.duckdb"))
    if not path.is_file():
        pytest.skip("build olist.duckdb first: uv run python -m pipeline --zip data/olist.zip --db olist.duckdb")
    return path


def test_why_question_quotes_each_cohort_from_its_own_orders(db: Path) -> None:
    events = list(ask("Why are late deliveries rated worse than on-time ones?", [], "full", db_path=db))
    answer = events[-1]
    assert isinstance(answer, Answer)
    assert answer.route == "answer", (answer.headline, answer.explanation, answer.trace)
    population = answer.trace["population"]
    assert population and population["with_cohort"], answer.trace
    assert answer.trace["figures_sql"] and "p_" in answer.trace["figures_sql"]
    assert answer.sql.startswith("WITH population AS"), answer.sql
    assert answer.quotes, (answer.explanation, answer.trace)
    cohorts = {q["cohort"] for q in answer.quotes}
    assert len(cohorts) >= 2, cohorts

    con = duckdb.connect(str(db), read_only=True)
    try:
        members = {
            (str(order_id), str(cohort))
            for order_id, cohort in con.execute(
                f"SELECT DISTINCT order_id, cohort FROM ({population['sql']}) AS p").fetchall()
        }
        for quote in answer.quotes:
            assert (quote["order_id"], quote["cohort"]) in members, quote
            text = con.execute(
                "SELECT text FROM clean.reviews WHERE review_id = ? AND order_id = ?",
                [quote["review_id"], quote["order_id"]]).fetchone()
            assert text and text[0] == quote["text"], quote
    finally:
        con.close()
    assert all(c["num_turns"] == 1 and not c["is_error"] for c in answer.trace["claude_calls"])

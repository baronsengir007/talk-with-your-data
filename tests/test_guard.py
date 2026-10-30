"""The guard with a population: figures may only read the p_* views of the same orders as the quotes."""

from pathlib import Path

import pytest

from analyst.warehouse import GuardError, Population, PopulationError, Warehouse, guard, guard_population

LATE_VS_ON_TIME = (
    "SELECT order_id, CASE WHEN is_late THEN 'late' ELSE 'on time' END AS cohort "
    "FROM clean.orders WHERE status = 'delivered' AND is_late IS NOT NULL"
)
POP = Population(sql=LATE_VS_ON_TIME, label="delivered orders, late vs on time", with_cohort=True)


def test_rejects_having_with_population() -> None:
    with pytest.raises(GuardError, match="no HAVING"):
        guard("SELECT cohort, count(*) AS n FROM p_orders GROUP BY cohort HAVING count(*) > 10", "clean", POP)


WHERE_CASES = [
    "SELECT COUNT(*) FROM p_orders WHERE is_late",
    "SELECT n FROM (SELECT COUNT(*) AS n FROM p_orders WHERE is_late) AS x",
    "WITH x AS (SELECT * FROM p_orders WHERE is_late) SELECT COUNT(*) FROM x",
    "SELECT COUNT(*) FILTER (WHERE is_late) AS n FROM p_orders",
]


def test_rejects_where_with_population() -> None:
    # One test (not parametrized) so the plan's check finds it by its plain name.
    for sql in WHERE_CASES:
        with pytest.raises(GuardError, match="no WHERE"):
            guard(sql, "clean", POP)


def test_customers_need_population_join() -> None:
    with pytest.raises(GuardError, match="clean.customers may only"):
        guard("SELECT count(*) FROM clean.customers", "clean", POP)
    with pytest.raises(GuardError, match="ON that uses a p_\\* view"):
        guard("SELECT count(*) FROM p_orders o JOIN clean.customers c ON c.n_orders > 1", "clean", POP)
    with pytest.raises(GuardError, match="ON that uses a p_\\* view"):
        guard("SELECT count(*) FROM p_orders o, clean.customers c", "clean", POP)
    ok = guard(
        "SELECT o.cohort, avg(c.n_orders) AS avg_orders FROM p_orders o "
        "JOIN clean.customers c ON c.customer_unique_id = o.customer_unique_id GROUP BY 1",
        "clean", POP,
    )
    assert ok.startswith("WITH population AS (SELECT DISTINCT order_id, cohort FROM (")
    assert "JOIN clean.customers AS c ON c.customer_unique_id = o.customer_unique_id" in ok
    assert guard("SELECT count(*) AS n FROM p_orders JOIN clean.sellers USING (seller_id)", "clean", POP)


BASE_TABLE_CASES = [
    ("SELECT COUNT(*) FROM clean.orders", "read p_orders instead of clean.orders"),
    ("SELECT COUNT(*) FROM clean.payments", "read p_payments instead of clean.payments"),
    ("SELECT COUNT(*) FROM clean.p_orders", "write it without a schema"),
    ("SELECT COUNT(*) FROM p_orders o JOIN clean.reviews r ON r.order_id = o.order_id", "read p_reviews"),
    ("SELECT COUNT(*) FROM orders", "not allowed with a population"),
    ("WITH p_orders AS (SELECT 1) SELECT * FROM p_orders", "taken by the population views"),
    ("SELECT * FROM read_csv('x.csv')", "table functions"),
]


def test_rejects_base_table_with_population() -> None:
    # One test (not parametrized) so the plan's check finds it by its plain name.
    for sql, reason in BASE_TABLE_CASES:
        with pytest.raises(GuardError, match=reason):
            guard(sql, "clean", POP)


def test_population_views_are_prepended_and_own_ctes_kept() -> None:
    out = guard(
        "WITH per_cohort AS (SELECT cohort, avg(review_score) AS avg_score FROM p_orders GROUP BY cohort) "
        "SELECT * FROM per_cohort ORDER BY cohort",
        "clean", POP,
    )
    for view in ("p_orders", "p_items", "p_payments", "p_reviews"):
        assert f"{view} AS (SELECT t.*, p.cohort FROM clean." in out
    assert out.index("population AS") < out.index("per_cohort AS")
    assert out.endswith("LIMIT 1000")
    without = Population(sql="SELECT order_id FROM clean.orders", label="all", with_cohort=False)
    assert "SELECT t.* FROM clean.orders AS t JOIN population AS p USING (order_id)" in guard(
        "SELECT count(*) AS n FROM p_orders", "clean", without)


def test_population_sql_gets_no_row_limit() -> None:
    assert "LIMIT" not in guard_population(LATE_VS_ON_TIME, "clean")
    with pytest.raises(GuardError, match="schema raw"):
        guard_population("SELECT order_id FROM raw.orders", "clean")
    with pytest.raises(GuardError, match="only a SELECT"):
        guard_population("DELETE FROM clean.orders", "clean")


def test_voucher_population_distinct(fixture_db: Path) -> None:
    """o1 has two voucher rows: the population lists it twice, the figures count it once."""
    wh = Warehouse("clean", fixture_db)
    try:
        pop = wh.population(
            "SELECT o.order_id FROM clean.orders o JOIN clean.payments p ON p.order_id = o.order_id "
            "WHERE p.payment_type = 'voucher'", "orders paid (partly) with a voucher")
        assert pop.with_cohort is False
        duplicates = wh.execute_fixed(f"SELECT count(*) AS n FROM ({pop.sql}) AS x").rows[0]["n"]
        assert duplicates == 2
        figures = wh.query("SELECT sum(order_payment_value) AS total, count(*) AS n_orders FROM p_orders", pop)
        golden = wh.execute_fixed(
            "SELECT sum(order_payment_value) AS total, count(*) AS n_orders FROM clean.orders WHERE order_id IN "
            "(SELECT DISTINCT order_id FROM clean.payments WHERE payment_type = 'voucher')").rows[0]
        assert figures.rows[0] == golden
        assert figures.rows[0]["n_orders"] == 1 and figures.rows[0]["total"] == pytest.approx(87.19)
        assert figures.sql.startswith("WITH population AS")
    finally:
        wh.close()


def test_population_needs_order_id_and_one_cohort_per_order(fixture_db: Path) -> None:
    wh = Warehouse("clean", fixture_db)
    try:
        with pytest.raises(PopulationError, match="order_id"):
            wh.population("SELECT customer_unique_id FROM clean.orders", "people")
        with pytest.raises(PopulationError, match="more than one cohort"):
            wh.population(
                "SELECT * FROM (SELECT order_id, 'a' AS cohort FROM clean.orders "
                "UNION ALL SELECT order_id, 'b' FROM clean.orders) AS both_cohorts",
                "every order in both cohorts")
        with pytest.raises(PopulationError, match="has no cohort"):
            wh.population("SELECT order_id, is_late AS cohort FROM clean.orders", "late flag, NULL when not delivered")
        pop = wh.population(LATE_VS_ON_TIME, "late vs on time")
        assert pop.with_cohort
        rows = wh.query("SELECT cohort, count(*) AS n FROM p_orders GROUP BY cohort ORDER BY cohort", pop).rows
        assert rows == [{"cohort": "late", "n": 1}, {"cohort": "on time", "n": 1}]
        assert wh.query("SELECT cohort, count(*) AS n FROM p_payments GROUP BY 1", pop).rows == [
            {"cohort": "on time", "n": 3}]
    finally:
        wh.close()

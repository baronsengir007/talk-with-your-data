"""SQL guard and the read-only warehouse."""

from pathlib import Path

import pytest

from analyst.warehouse import GuardError, Warehouse, guard


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("DROP TABLE orders", "only a SELECT"),
        ("DELETE FROM clean.orders", "only a SELECT"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("SELECT * FROM read_csv('x.csv')", "table functions"),
        ("SELECT * FROM query('DROP TABLE x')", "table functions"),
        ("SELECT glob('*')", "function glob"),
        ("SELECT * FROM raw.orders", "schema raw is not allowed"),
        ("SELECT * FROM clean.orders o JOIN raw.customers c ON c.customer_id = o.order_id", "schema raw"),
        ("SELECT * FROM orders", "must be read as clean.orders"),
        ("COPY clean.orders TO 'x.csv'", "only a SELECT"),
        ("PRAGMA database_list", "only a SELECT"),
    ],
)
def test_guard_rejects(sql: str, reason: str) -> None:
    with pytest.raises(GuardError, match=reason):
        guard(sql, "clean")


def test_guard_allows_one_select_on_the_schema_and_adds_a_limit() -> None:
    out = guard("SELECT count(*) AS n FROM clean.orders WHERE year(purchase_ts) = 2017", "clean")
    assert out.startswith("SELECT COUNT(*) AS n FROM clean.orders")
    assert out.endswith("LIMIT 1000")


def test_guard_allows_ctes_and_caps_a_large_limit() -> None:
    assert guard("WITH x AS (SELECT * FROM clean.orders) SELECT count(*) FROM x", "clean").endswith("LIMIT 1000")
    assert guard("SELECT * FROM clean.orders LIMIT 5", "clean").endswith("LIMIT 5")
    assert guard("SELECT * FROM clean.orders LIMIT 5000", "clean").endswith("LIMIT 1000")


def test_naive_mode_reads_raw_only() -> None:
    assert "raw.orders" in guard("SELECT count(*) FROM raw.orders", "raw")
    with pytest.raises(GuardError, match="schema clean"):
        guard("SELECT count(*) FROM clean.orders", "raw")


def test_warehouse_queries_read_only(fixture_db: Path) -> None:
    wh = Warehouse("clean", fixture_db)
    try:
        result = wh.query("SELECT count(*) AS n FROM clean.orders WHERE year(purchase_ts) = 2018")
        assert result.columns == ["n"]
        assert result.rows == [{"n": 2}]
        assert result.sql.endswith("LIMIT 1000")
        assert "clean.orders(" in wh.schema_text()
        with pytest.raises(GuardError):
            wh.query("CREATE TABLE clean.x AS SELECT 1")
        with pytest.raises(Exception):  # the connection itself is read-only, even past the guard
            wh._con.execute("CREATE TABLE clean.x AS SELECT 1")
    finally:
        wh.close()


def test_warehouse_needs_an_existing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Warehouse("clean", tmp_path / "missing.duckdb")

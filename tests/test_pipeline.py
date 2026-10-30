"""The pipeline on the tiny fixture: clean layer shape, embedding cache, lineage and reconciliation."""

import shutil
import zipfile
from pathlib import Path

import duckdb
import pytest

from pipeline import ReconciliationError, build
from pipeline import build_db
from pipeline.__main__ import main
from tests.conftest import fake_embed

RAW_TABLES = {
    "customers", "geolocation", "order_items", "order_payments", "order_reviews",
    "orders", "products", "sellers", "product_category_name_translation",
}
CLEAN_TABLES = {"orders", "order_items", "payments", "reviews", "customers", "sellers"}


def _build(csv_dir: Path, folder: Path, embed=fake_embed) -> Path:
    db = folder / "olist.duckdb"
    build(csv_dir, db, embeddings_path=folder / "review_embeddings.parquet", embed=embed)
    return db


def _raw_without_customer_c3(fixture_csv_dir: Path, tmp_path: Path) -> Path:
    """Copy of the fixture where customer c3 is gone, so order o3 is lost by the join."""
    broken = tmp_path / "raw"
    shutil.copytree(fixture_csv_dir, broken)
    customers = broken / "olist_customers_dataset.csv"
    lines = customers.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.startswith("c3,")]
    assert len(kept) == len(lines) - 1
    customers.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return broken


def _zip_of(csv_dir: Path, zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w") as archive:
        for csv in csv_dir.iterdir():
            archive.write(csv, csv.name)
    return zip_path


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> duckdb.DuckDBPyConnection:
    from tests.conftest import FIXTURES

    db = _build(FIXTURES, tmp_path_factory.mktemp("built"))
    con = duckdb.connect(str(db), read_only=True)
    yield con
    con.close()


def test_build_writes_raw_and_clean_tables(built) -> None:
    tables = built.execute(
        "SELECT table_schema, table_name FROM information_schema.tables").fetchall()
    assert {t for s, t in tables if s == "raw"} == RAW_TABLES
    assert {t for s, t in tables if s == "clean"} == CLEAN_TABLES


def test_clean_orders_columns_and_delivery_facts(built) -> None:
    types = dict(built.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'clean' AND table_name = 'orders'").fetchall())
    assert types["purchase_ts"] == "TIMESTAMP"
    assert types["is_late"] == "BOOLEAN"
    assert "payment_type" not in types, "payment type is never a column on orders"
    rows = {r[0]: r for r in built.execute(
        "SELECT order_id, status, delivery_days, days_vs_promise, is_late, review_score, "
        "n_items, n_sellers, order_payment_value, n_payment_types, customer_state, customer_unique_id "
        "FROM clean.orders").fetchall()}
    assert rows["o1"][1:] == ("delivered", 8, -8, False, 5, 1, 1, pytest.approx(87.19), 2, "SP", "u1")
    assert rows["o2"][1:] == ("delivered", 27, 7, True, 3, 0, 0, None, 0, "SP", "u2")
    # shipped, no delivery date: the delivery columns stay empty
    assert rows["o3"][1:6] == ("shipped", None, None, None, None)


def test_distance_uses_averaged_geolocation(built) -> None:
    km = built.execute("SELECT distance_km FROM clean.orders WHERE order_id = 'o1'").fetchone()[0]
    assert 80 < km < 100  # sao paulo (01037, two rows averaged) to campinas (13023)
    assert built.execute("SELECT distance_km FROM clean.orders WHERE order_id = 'o2'").fetchone()[0] is None


def test_reviews_keep_the_latest_per_order(built) -> None:
    rows = built.execute(
        "SELECT order_id, review_id, score, text, embedding IS NOT NULL FROM clean.reviews ORDER BY 1").fetchall()
    assert rows == [
        ("o1", "r1", 5, "Recebi bem antes do prazo estipulado.", True),
        ("o2", "r3", 3, None, False),
    ]
    dim = built.execute("SELECT len(embedding) FROM clean.reviews WHERE order_id = 'o1'").fetchone()[0]
    assert dim == build_db.EMBEDDING_DIM


def test_order_items_carry_the_english_category(built) -> None:
    row = built.execute(
        "SELECT category, category_english, price, freight_value FROM clean.order_items").fetchone()
    assert row == ("perfumaria", "perfumery", pytest.approx(58.90), pytest.approx(13.29))


def test_payments_and_customers(built) -> None:
    assert built.execute("SELECT payment_type, payment_value FROM clean.payments ORDER BY 2").fetchall() == [
        ("voucher", pytest.approx(5.0)), ("voucher", pytest.approx(10.0)), ("credit_card", pytest.approx(72.19))]
    customers = built.execute(
        "SELECT customer_unique_id, n_orders, customer_city FROM clean.customers ORDER BY 1").fetchall()
    assert customers == [("u1", 1, "sao paulo"), ("u2", 1, "sao bernardo do campo"), ("u3", 1, "sao paulo")]


def test_embedding_cache_skips_known_texts(fixture_csv_dir: Path, tmp_path: Path) -> None:
    calls: list[int] = []

    def counting_embed(texts):
        calls.append(len(texts))
        return fake_embed(texts)

    _build(fixture_csv_dir, tmp_path, embed=counting_embed)
    parquet = tmp_path / "review_embeddings.parquet"
    assert parquet.is_file()
    assert calls == [1]  # only o1 has a kept review with text
    _build(fixture_csv_dir, tmp_path, embed=counting_embed)
    assert calls == [1], "second build must embed nothing"

    changed = tmp_path / "raw"
    shutil.copytree(fixture_csv_dir, changed)
    reviews = changed / "olist_order_reviews_dataset.csv"
    reviews.write_text(reviews.read_text(encoding="utf-8").replace("Recebi bem", "Recebi muito"), encoding="utf-8")
    _build(changed, tmp_path, embed=counting_embed)
    assert calls == [1, 1], "a changed text is embedded again"
    rows = duckdb.connect().execute("SELECT count(*) FROM read_parquet(?)", [parquet.as_posix()]).fetchone()[0]
    assert rows == 2, "the cache keeps both versions"


def test_wrong_vector_size_fails_the_build(fixture_csv_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ReconciliationError, match="wrong number or size"):
        _build(fixture_csv_dir, tmp_path, embed=lambda texts: [[0.0] * 3 for _ in texts])


def test_every_raw_column_is_in_lineage(built) -> None:
    lineage = build_db.load_lineage()
    build_db._check_lineage(built, lineage)
    del lineage["raw.orders"]["order_status"]
    with pytest.raises(ReconciliationError, match="raw.orders.order_status is not in lineage"):
        build_db._check_lineage(built, lineage)
    lineage = build_db.load_lineage()
    lineage["raw.sellers"]["seller_city"]["to"] = ["clean.sellers.city"]
    with pytest.raises(ReconciliationError, match="no such clean column"):
        build_db._check_lineage(built, lineage)


def test_dropped_order_fails_the_build(fixture_csv_dir: Path, tmp_path: Path) -> None:
    broken = _raw_without_customer_c3(fixture_csv_dir, tmp_path)
    with pytest.raises(ReconciliationError, match="raw has 3, clean has 2"):
        _build(broken, tmp_path)


def test_build_replaces_an_existing_file(fixture_csv_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "olist.duckdb"
    db.write_bytes(b"not a database")
    _build(fixture_csv_dir, tmp_path)
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM raw.orders").fetchone()[0] == 3
    finally:
        con.close()


def test_cli_builds_from_zip(fixture_csv_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(build_db, "_fastembed", fake_embed)
    zip_path = _zip_of(fixture_csv_dir, tmp_path / "olist.zip")
    db = tmp_path / "out.duckdb"
    parquet = tmp_path / "emb.parquet"
    assert main(["--zip", str(zip_path), "--db", str(db), "--embeddings", str(parquet)]) == 0
    assert db.is_file() and parquet.is_file()


def test_cli_exits_non_zero_on_reconciliation_failure(fixture_csv_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(build_db, "_fastembed", fake_embed)
    broken = _raw_without_customer_c3(fixture_csv_dir, tmp_path)
    zip_path = _zip_of(broken, tmp_path / "olist.zip")
    assert main(["--zip", str(zip_path), "--db", str(tmp_path / "out.duckdb"),
                 "--embeddings", str(tmp_path / "emb.parquet")]) == 1

"""Build olist.duckdb: the raw layer as loaded, the clean star schema, review embeddings, reconciliation.

build(csv_dir, db_path, embeddings_path, embed) writes a new file (an old one is deleted first).
Schema `raw` holds the 9 CSVs as loaded, every column as text, table names
without the `olist_` prefix and `_dataset` suffix. Schema `clean` holds the
star schema (ADR 0003): orders, order_items, payments, reviews, customers,
sellers. Every raw column is accounted for in lineage.yaml (where it went,
or why it was left out). Review texts get an embedding (ADR 0005) that is
cached in a parquet file keyed by order_id and the md5 of the text, so a
later build only embeds texts that are new. A reconciliation check that
fails raises ReconciliationError, so a build never leaves a clean layer that
lost rows or changed a total.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import duckdb
import yaml

log = logging.getLogger(__name__)

RAW_FILES = {
    "customers": "olist_customers_dataset.csv",
    "geolocation": "olist_geolocation_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "order_payments": "olist_order_payments_dataset.csv",
    "order_reviews": "olist_order_reviews_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "products": "olist_products_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "product_category_name_translation": "product_category_name_translation.csv",
}

LINEAGE_PATH = Path(__file__).with_name("lineage.yaml")
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIM = 384
EMBED_BATCH = 256

# embed(texts) -> one vector of EMBEDDING_DIM floats per text, in the same order.
Embedder = Callable[[Sequence[str]], list[list[float]]]


class ReconciliationError(Exception):
    """A check between the raw layer and the clean layer failed; the build is not usable."""


def build(
    csv_dir: str | Path,
    db_path: str | Path,
    embeddings_path: str | Path | None = None,
    embed: Embedder | None = None,
) -> None:
    """Write a fresh DuckDB file at db_path from the 9 CSVs in csv_dir.

    embeddings_path: the parquet cache of review vectors (read if present, written
    when new texts were embedded). None means no cache: every text is embedded.
    embed: the embedding function; None means fastembed with EMBEDDING_MODEL.
    """
    csv_dir = Path(csv_dir)
    db_path = Path(db_path)
    missing = [name for name in RAW_FILES.values() if not (csv_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"raw CSV files missing in {csv_dir}: {', '.join(missing)}")
    for stale in (db_path, db_path.with_name(db_path.name + ".wal")):
        if stale.exists():
            stale.unlink()

    con = duckdb.connect(str(db_path))
    try:
        _load_raw(con, csv_dir)
        con.execute("CREATE SCHEMA clean")
        _build_kept_reviews(con)
        _build_embeddings(con, Path(embeddings_path) if embeddings_path else None, embed or _fastembed)
        _build_clean(con)
        _reconcile(con)
    finally:
        con.close()
    log.info("built %s", db_path)


def _load_raw(con: duckdb.DuckDBPyConnection, csv_dir: Path) -> None:
    con.execute("CREATE SCHEMA raw")
    for table, filename in RAW_FILES.items():
        path = (csv_dir / filename).as_posix()
        # all_varchar: the raw layer is the CSV as delivered; the clean layer casts.
        con.execute(
            f"CREATE TABLE raw.{table} AS "
            "SELECT * FROM read_csv(?, header = true, all_varchar = true)",
            [path],
        )
        count = con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
        log.info("raw.%s: %d rows", table, count)


def _build_kept_reviews(con: duckdb.DuckDBPyConnection) -> None:
    """One review per order: the latest (creation date, then answer time, then id)."""
    con.execute(
        """
        CREATE TEMP TABLE kept_reviews AS
        SELECT
            review_id,
            order_id,
            CAST(review_score AS INTEGER)                      AS score,
            nullif(trim(review_comment_title), '')             AS title,
            nullif(trim(review_comment_message), '')           AS text,
            CAST(review_creation_date AS TIMESTAMP)            AS created_ts,
            CAST(review_answer_timestamp AS TIMESTAMP)         AS answered_ts
        FROM raw.order_reviews
        QUALIFY row_number() OVER (
            PARTITION BY order_id
            ORDER BY review_creation_date DESC, review_answer_timestamp DESC, review_id
        ) = 1
        """
    )


def _build_embeddings(con: duckdb.DuckDBPyConnection, parquet: Path | None, embed: Embedder) -> None:
    """Fill temp table `embeddings` (order_id, text_md5, embedding) for every kept review with text.

    Vectors come from the parquet cache when the (order_id, md5) pair is there,
    otherwise from `embed`; new vectors are appended to the cache.
    """
    # A list column, cast to FLOAT[384] after the join: DuckDB 2.0.0 corrupts a
    # fixed-size ARRAY column when a LEFT JOIN leaves it unmatched.
    con.execute("CREATE TEMP TABLE embeddings (order_id VARCHAR, text_md5 VARCHAR, embedding FLOAT[])")
    if parquet is not None and parquet.is_file():
        con.execute(
            "INSERT INTO embeddings SELECT order_id, text_md5, embedding FROM read_parquet(?)",
            [parquet.as_posix()],
        )
        log.info("embeddings: %d cached vectors in %s", _count(con, "embeddings"), parquet)
    todo = con.execute(
        """
        SELECT k.order_id, md5(k.text), k.text
        FROM kept_reviews k
        WHERE k.text IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM embeddings e WHERE e.order_id = k.order_id AND e.text_md5 = md5(k.text))
        ORDER BY k.order_id
        """
    ).fetchall()
    log.info("embeddings: %d texts to embed", len(todo))
    if not todo:
        return
    for start in range(0, len(todo), EMBED_BATCH):
        batch = todo[start : start + EMBED_BATCH]
        vectors = embed([text for _, _, text in batch])
        if len(vectors) != len(batch) or any(len(v) != EMBEDDING_DIM for v in vectors):
            raise ReconciliationError("embeddings: the embedder returned the wrong number or size of vectors")
        con.executemany(
            "INSERT INTO embeddings VALUES (?, ?, ?)",
            [(order_id, text_md5, list(map(float, v))) for (order_id, text_md5, _), v in zip(batch, vectors)],
        )
        log.info("embeddings: %d / %d", min(start + EMBED_BATCH, len(todo)), len(todo))
    if parquet is not None:
        parquet.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            "COPY (SELECT order_id, text_md5, embedding FROM embeddings ORDER BY order_id, text_md5) "
            "TO ? (FORMAT PARQUET)",
            [parquet.as_posix()],
        )
        log.info("embeddings: cache written to %s", parquet)


def _fastembed(texts: Sequence[str]) -> list[list[float]]:
    """Default embedder: fastembed, ONNX, cache dir .models/ (env FASTEMBED_CACHE_PATH)."""
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(EMBEDDING_MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", ".models"))
    return [vector.tolist() for vector in _model.embed(list(texts), batch_size=64)]


_model = None  # the fastembed model, loaded once per process


def _build_clean(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TEMP TABLE geo AS
        SELECT
            geolocation_zip_code_prefix        AS zip_prefix,
            avg(CAST(geolocation_lat AS DOUBLE)) AS lat,
            avg(CAST(geolocation_lng AS DOUBLE)) AS lng
        FROM raw.geolocation
        GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TABLE clean.sellers AS
        SELECT
            seller_id,
            seller_zip_code_prefix AS seller_zip_prefix,
            seller_city,
            seller_state
        FROM raw.sellers
        """
    )
    con.execute(
        """
        CREATE TABLE clean.order_items AS
        SELECT
            i.order_id,
            CAST(i.order_item_id AS INTEGER)            AS order_item_id,
            i.product_id,
            i.seller_id,
            CAST(i.shipping_limit_date AS TIMESTAMP)    AS shipping_limit_ts,
            CAST(i.price AS DOUBLE)                     AS price,
            CAST(i.freight_value AS DOUBLE)             AS freight_value,
            p.product_category_name                     AS category,
            coalesce(t.product_category_name_english, p.product_category_name) AS category_english
        FROM raw.order_items i
        LEFT JOIN raw.products p ON p.product_id = i.product_id
        LEFT JOIN raw.product_category_name_translation t
               ON t.product_category_name = p.product_category_name
        """
    )
    con.execute(
        """
        CREATE TABLE clean.payments AS
        SELECT
            order_id,
            CAST(payment_sequential AS INTEGER)   AS payment_sequential,
            payment_type,
            CAST(payment_installments AS INTEGER) AS installments,
            CAST(payment_value AS DOUBLE)         AS payment_value
        FROM raw.order_payments
        """
    )
    con.execute(
        f"""
        CREATE TABLE clean.reviews AS
        SELECT
            k.review_id, k.order_id, k.score, k.title, k.text, k.created_ts, k.answered_ts,
            CAST(e.embedding AS FLOAT[{EMBEDDING_DIM}]) AS embedding
        FROM kept_reviews k
        LEFT JOIN embeddings e ON e.order_id = k.order_id AND e.text_md5 = md5(k.text)
        """
    )
    con.execute(
        """
        CREATE TABLE clean.orders AS
        WITH item_facts AS (
            SELECT
                i.order_id,
                count(*)                     AS n_items,
                count(DISTINCT i.seller_id)  AS n_sellers,
                sum(i.price)                 AS items_value,
                sum(i.freight_value)         AS freight_value
            FROM clean.order_items i
            GROUP BY 1
        ),
        seller_distance AS (
            -- haversine km between the order's buyer and each distinct seller, averaged per order
            SELECT
                os.order_id,
                avg(2 * 6371 * asin(sqrt(
                    power(sin(radians(sg.lat - cg.lat) / 2), 2)
                    + cos(radians(cg.lat)) * cos(radians(sg.lat))
                    * power(sin(radians(sg.lng - cg.lng) / 2), 2)))) AS distance_km
            FROM (SELECT DISTINCT order_id, seller_id FROM clean.order_items) os
            JOIN raw.orders o        ON o.order_id = os.order_id
            JOIN raw.customers c     ON c.customer_id = o.customer_id
            JOIN raw.sellers s       ON s.seller_id = os.seller_id
            JOIN geo cg              ON cg.zip_prefix = c.customer_zip_code_prefix
            JOIN geo sg              ON sg.zip_prefix = s.seller_zip_code_prefix
            GROUP BY 1
        ),
        payment_facts AS (
            SELECT order_id, sum(payment_value) AS order_payment_value,
                   count(DISTINCT payment_type) AS n_payment_types
            FROM clean.payments
            GROUP BY 1
        ),
        cast_orders AS (
            SELECT
                o.order_id,
                c.customer_unique_id,
                c.customer_zip_code_prefix                           AS customer_zip_prefix,
                c.customer_city,
                c.customer_state,
                o.order_status                                       AS status,
                CAST(o.order_purchase_timestamp AS TIMESTAMP)        AS purchase_ts,
                CAST(o.order_approved_at AS TIMESTAMP)               AS approved_ts,
                CAST(o.order_delivered_carrier_date AS TIMESTAMP)    AS carrier_ts,
                CAST(o.order_delivered_customer_date AS TIMESTAMP)   AS delivered_ts,
                CAST(o.order_estimated_delivery_date AS TIMESTAMP)   AS estimated_ts
            FROM raw.orders o
            JOIN raw.customers c ON c.customer_id = o.customer_id
        )
        SELECT
            o.order_id, o.customer_unique_id, o.customer_zip_prefix, o.customer_city, o.customer_state,
            o.status, o.purchase_ts, o.approved_ts, o.carrier_ts, o.delivered_ts, o.estimated_ts,
            -- delivery columns only for delivered orders that have a delivery date
            CASE WHEN o.status = 'delivered' AND o.delivered_ts IS NOT NULL
                 THEN date_diff('day', o.purchase_ts, o.delivered_ts) END          AS delivery_days,
            CASE WHEN o.status = 'delivered' AND o.delivered_ts IS NOT NULL
                 THEN date_diff('day', o.estimated_ts, o.delivered_ts) END         AS days_vs_promise,
            CASE WHEN o.status = 'delivered' AND o.delivered_ts IS NOT NULL
                 THEN o.delivered_ts > o.estimated_ts END                          AS is_late,
            r.score                                                                AS review_score,
            coalesce(f.n_items, 0)                                                 AS n_items,
            coalesce(f.n_sellers, 0)                                               AS n_sellers,
            f.items_value,
            f.freight_value,
            p.order_payment_value,
            coalesce(p.n_payment_types, 0)                                         AS n_payment_types,
            d.distance_km
        FROM cast_orders o
        LEFT JOIN item_facts f      ON f.order_id = o.order_id
        LEFT JOIN seller_distance d ON d.order_id = o.order_id
        LEFT JOIN payment_facts p   ON p.order_id = o.order_id
        LEFT JOIN clean.reviews r   ON r.order_id = o.order_id
        """
    )
    con.execute(
        """
        CREATE TABLE clean.customers AS
        -- one row per person; location is the location of the person's latest order
        SELECT
            customer_unique_id,
            count(*)                        AS n_orders,
            min(purchase_ts)                AS first_order_ts,
            max(purchase_ts)                AS last_order_ts,
            arg_max(customer_zip_prefix, (purchase_ts, order_id)) AS customer_zip_prefix,
            arg_max(customer_city, (purchase_ts, order_id))       AS customer_city,
            arg_max(customer_state, (purchase_ts, order_id))      AS customer_state
        FROM clean.orders
        GROUP BY 1
        """
    )
    for table in ("orders", "order_items", "payments", "reviews", "customers", "sellers"):
        log.info("clean.%s: %d rows", table, _count(con, f"clean.{table}"))


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _scalar(con: duckdb.DuckDBPyConnection, sql: str):
    return con.execute(sql).fetchone()[0]


def _reconcile(con: duckdb.DuckDBPyConnection) -> None:
    """Prove nothing was lost or changed between raw and clean. Raise on the first mismatch."""
    checks: list[tuple[str, object, object]] = []

    def same(name: str, left: object, right: object) -> None:
        checks.append((name, left, right))
        if left != right:
            raise ReconciliationError(f"{name}: raw has {left}, clean has {right}")

    # Row counts.
    same("orders rows", _count(con, "raw.orders"), _count(con, "clean.orders"))
    same("orders distinct ids", _count(con, "clean.orders"), _scalar(con, "SELECT count(DISTINCT order_id) FROM clean.orders"))
    same("order_items rows", _count(con, "raw.order_items"), _count(con, "clean.order_items"))
    same("payments rows", _count(con, "raw.order_payments"), _count(con, "clean.payments"))
    same("sellers rows", _count(con, "raw.sellers"), _count(con, "clean.sellers"))
    same("customers rows", _scalar(con, "SELECT count(DISTINCT customer_unique_id) FROM raw.customers"),
         _count(con, "clean.customers"))
    same("customers orders", _count(con, "clean.orders"), _scalar(con, "SELECT sum(n_orders) FROM clean.customers"))
    raw_reviews = _count(con, "raw.order_reviews")
    review_orders = _scalar(con, "SELECT count(DISTINCT order_id) FROM raw.order_reviews")
    clean_reviews = _count(con, "clean.reviews")
    same("reviews one per order", review_orders, clean_reviews)
    same("reviews rows", raw_reviews, clean_reviews + (raw_reviews - review_orders))
    log.info("reconciliation: reviews raw %d = clean %d + %d dropped (older reviews of the same order)",
             raw_reviews, clean_reviews, raw_reviews - review_orders)

    # Totals.
    same("order_items sum price",
         _scalar(con, "SELECT round(sum(CAST(price AS DOUBLE)), 2) FROM raw.order_items"),
         _scalar(con, "SELECT round(sum(price), 2) FROM clean.order_items"))
    raw_paid = _scalar(con, "SELECT round(sum(CAST(payment_value AS DOUBLE)), 2) FROM raw.order_payments")
    same("payments sum value", raw_paid, _scalar(con, "SELECT round(sum(payment_value), 2) FROM clean.payments"))
    same("orders sum payment value", raw_paid, _scalar(con, "SELECT round(sum(order_payment_value), 2) FROM clean.orders"))
    same("payments count by type",
         con.execute("SELECT payment_type, count(*) FROM raw.order_payments GROUP BY 1 ORDER BY 1").fetchall(),
         con.execute("SELECT payment_type, count(*) FROM clean.payments GROUP BY 1 ORDER BY 1").fetchall())
    same("reviews count by score (kept rows)",
         con.execute("SELECT score, count(*) FROM kept_reviews GROUP BY 1 ORDER BY 1").fetchall(),
         con.execute("SELECT score, count(*) FROM clean.reviews GROUP BY 1 ORDER BY 1").fetchall())
    same("orders count by review_score",
         con.execute("SELECT score, count(*) FROM clean.reviews GROUP BY 1 ORDER BY 1").fetchall(),
         con.execute("SELECT review_score, count(*) FROM clean.orders WHERE review_score IS NOT NULL "
                     "GROUP BY 1 ORDER BY 1").fetchall())

    # Embeddings: every review with text has a vector.
    same("reviews with text have an embedding",
         _scalar(con, "SELECT count(*) FROM clean.reviews WHERE text IS NOT NULL"),
         _scalar(con, "SELECT count(*) FROM clean.reviews WHERE embedding IS NOT NULL"))

    # Lineage: every raw column is accounted for; every target column exists.
    _check_lineage(con)
    log.info("reconciliation: %d checks passed", len(checks) + 1)


def _check_lineage(con: duckdb.DuckDBPyConnection, lineage: dict | None = None) -> None:
    lineage = lineage if lineage is not None else load_lineage()
    raw_columns = con.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'raw'"
    ).fetchall()
    clean_columns = {
        f"clean.{t}.{c}" for t, c in con.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'clean'"
        ).fetchall()
    }
    problems = []
    for table, column in raw_columns:
        entry = lineage.get(f"raw.{table}", {}).get(column)
        if entry is None:
            problems.append(f"raw.{table}.{column} is not in lineage.yaml")
            continue
        targets = entry.get("to") or []
        if not targets and not entry.get("dropped"):
            problems.append(f"raw.{table}.{column} has neither `to` nor `dropped`")
        for target in targets:
            if target not in clean_columns:
                problems.append(f"raw.{table}.{column} -> {target}: no such clean column")
    if problems:
        raise ReconciliationError("lineage: " + "; ".join(problems))


def load_lineage(path: Path = LINEAGE_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)

"""Warehouse(schema): read-only queries on olist.duckdb behind the SQL guard.

The connection is opened read-only with external access off and the
configuration locked, so SQL can never write, read files or reach the
network. The guard (sqlglot) allows exactly one SELECT on tables of the
warehouse's schema, rejects table functions, and adds a row limit.

Population: when the plan names a group of orders (`population_sql`, one
SELECT on clean returning `order_id` and optionally `cohort`), code wraps
it into the views p_orders, p_items, p_payments and p_reviews (distinct
order ids, one cohort per order) and prepends them to the figures SQL. The
figures SQL may then read only those views (plus clean.customers and
clean.sellers joined to them) and may not contain WHERE or HAVING, so the
figures and the quotes always come from the same orders.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

DEFAULT_DB = "olist.duckdb"
MAX_ROWS = 1000
QUERY_TIMEOUT_S = 10.0

# DuckDB functions that read files, run SQL text or reach outside the database.
# The connection blocks them too (enable_external_access=false); the guard names
# them so the refusal is explicit and testable.
TABLE_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_json_objects", "read_ndjson", "read_ndjson_auto", "read_text", "read_blob",
    "parquet_scan", "csv_scan", "json_scan", "glob", "query", "query_table",
    "sqlite_scan", "postgres_scan", "duckdb_settings",
}

# The views of a population: view name -> the clean table it cuts.
P_TABLES = {
    "p_orders": "orders",
    "p_items": "order_items",
    "p_payments": "payments",
    "p_reviews": "reviews",
}
# Dimension tables (no order_id) that may join a p_* view.
JOINABLE = {"customers", "sellers"}
POPULATION_HINT = "put every narrowing in population_sql or in its cohort column"


class GuardError(Exception):
    """The SQL is not allowed. The message says why, in words the page can show."""


class PopulationError(GuardError):
    """The population SQL does not define one group of orders with one cohort each."""


@dataclass(frozen=True)
class Result:
    columns: list[str]
    rows: list[dict]
    sql: str  # what ran, after the guard added the population views and the row limit


@dataclass(frozen=True)
class Population:
    """One group of orders, checked by the warehouse: the source of figures and quotes."""

    sql: str  # the checked SELECT on clean that returns order_id[, cohort]
    label: str
    with_cohort: bool

    def ctes(self) -> list[tuple[str, str]]:
        """The WITH clause the warehouse prepends: (name, SELECT) in order."""
        cols = "order_id, cohort" if self.with_cohort else "order_id"
        extra = ", p.cohort" if self.with_cohort else ""
        ctes = [("population", f"SELECT DISTINCT {cols} FROM ({self.sql}) AS population_rows")]
        for view, table in P_TABLES.items():
            ctes.append(
                (view, f"SELECT t.*{extra} FROM clean.{table} AS t JOIN population AS p USING (order_id)")
            )
        return ctes

    def with_clause(self) -> str:
        return "WITH " + ", ".join(f"{name} AS ({sql})" for name, sql in self.ctes())


def _parse_select(sql: str) -> exp.Select:
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise GuardError(f"SQL could not be parsed: {exc}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise GuardError(f"exactly one statement is allowed, got {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        kind = type(tree).__name__.upper()
        raise GuardError(f"only a SELECT is allowed, not {kind}")
    return tree


def _check_functions(tree: exp.Expression) -> None:
    for func in tree.find_all(exp.Func):
        fname = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if fname in TABLE_FUNCTIONS:
            raise GuardError(f"function {fname} is not allowed")


def _plain_table(table: exp.Table) -> None:
    if isinstance(table.this, exp.Func) or not isinstance(table.this, exp.Identifier):
        raise GuardError("table functions are not allowed")


def _check_schema_tables(tree: exp.Select, schema: str, cte_names: set[str]) -> None:
    """Every table is `schema.table` or a CTE of the statement itself."""
    for table in tree.find_all(exp.Table):
        _plain_table(table)
        name = table.name.lower()
        table_schema = (table.db or "").lower()
        if table_schema == "":
            if name in cte_names:
                continue
            raise GuardError(f"table {name} must be read as {schema}.{name}")
        if table_schema != schema:
            raise GuardError(f"schema {table_schema} is not allowed; only {schema}")


def _check_population_tables(tree: exp.Select, schema: str, cte_names: set[str]) -> None:
    """With a population: only p_* views, own CTEs, and customers/sellers joined to a p_* view."""
    for table in tree.find_all(exp.Table):
        _plain_table(table)
        name = table.name.lower()
        table_schema = (table.db or "").lower()
        if table_schema == "" and (name in P_TABLES or name in cte_names):
            continue
        if table_schema == schema and name in JOINABLE:
            _check_joined_to_population(table, schema)
            continue
        if name in P_TABLES:
            raise GuardError(f"{name} is a view of the population; write it without a schema")
        for view, base in P_TABLES.items():
            if table_schema == schema and name == base:
                raise GuardError(f"with a population, read {view} instead of {schema}.{base}")
        shown = f"{table_schema}.{name}" if table_schema else name
        raise GuardError(
            f"table {shown} is not allowed with a population; read "
            f"{', '.join(P_TABLES)} (plus {schema}.customers or {schema}.sellers joined to them)"
        )


def _check_joined_to_population(table: exp.Table, schema: str) -> None:
    where = f"{schema}.{table.name}"
    join = table.parent
    if not isinstance(join, exp.Join):
        raise GuardError(f"{where} may only be the right side of a JOIN whose ON uses a p_* view")
    select = join.parent
    sources: list[exp.Table] = []
    if isinstance(select, exp.Select):
        if select.args.get("from_"):
            sources.extend(select.args["from_"].find_all(exp.Table))
        for other in select.args.get("joins") or []:
            sources.extend(other.find_all(exp.Table))
    p_aliases = {
        t.alias_or_name.lower() for t in sources if (t.db or "") == "" and t.name.lower() in P_TABLES
    }
    if not p_aliases:
        raise GuardError(f"{where} may only be joined to a p_* view in the same SELECT")
    if join.args.get("using"):
        return
    on = join.args.get("on")
    if on is None or not any(c.table.lower() in p_aliases for c in on.find_all(exp.Column)):
        raise GuardError(f"the JOIN of {where} must have an ON that uses a p_* view")


def _with_limit(tree: exp.Select) -> exp.Select:
    limit = tree.args.get("limit")
    if (
        limit is None
        or not isinstance(limit.expression, exp.Literal)
        or int(limit.expression.this) > MAX_ROWS
    ):
        tree = tree.limit(MAX_ROWS)
    return tree


def guard(sql: str, schema: str, population: Population | None = None) -> str:
    """Return the SQL that may run, or raise GuardError.

    Allowed: one SELECT (CTEs included) that reads only tables of `schema`
    or names defined in its own WITH clause. Rejected: anything else, any
    other schema, table functions, and more than one statement.

    With a population: the SELECT reads only the p_* views (and
    clean.customers / clean.sellers joined to them), has no WHERE and no
    HAVING at any level (FILTER counts as WHERE), and gets the population's
    WITH clause prepended, so what runs are the figures of that population.
    """
    tree = _parse_select(sql)
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    if population is None:
        _check_schema_tables(tree, schema, cte_names)
        _check_functions(tree)
        return _with_limit(tree).sql(dialect="duckdb")

    taken = cte_names & (set(P_TABLES) | {"population"})
    if taken:
        raise GuardError(f"CTE name {sorted(taken)[0]} is taken by the population views")
    if tree.find(exp.Where):
        raise GuardError(
            f"with a population, no WHERE (or FILTER) is allowed in the figures SQL; {POPULATION_HINT}"
        )
    if tree.find(exp.Having):
        raise GuardError(f"with a population, no HAVING is allowed in the figures SQL; {POPULATION_HINT}")
    _check_population_tables(tree, schema, cte_names)
    _check_functions(tree)

    own = tree.args.get("with_")
    ctes = [
        exp.CTE(
            this=sqlglot.parse_one(cte_sql, read="duckdb"),
            alias=exp.TableAlias(this=exp.to_identifier(name)),
        )
        for name, cte_sql in population.ctes()
    ]
    tree.set("with_", exp.With(expressions=[*ctes, *(own.expressions if own else [])]))
    return _with_limit(tree).sql(dialect="duckdb")


def guard_population(sql: str, schema: str) -> str:
    """The population SQL: one SELECT on `schema`, no table functions, no row limit added."""
    tree = _parse_select(sql)
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    _check_schema_tables(tree, schema, cte_names)
    _check_functions(tree)
    return tree.sql(dialect="duckdb")


class Warehouse:
    """Read-only window on one schema of olist.duckdb."""

    def __init__(self, schema: str, db_path: str | Path | None = None) -> None:
        if schema not in ("raw", "clean"):
            raise ValueError(f"unknown schema {schema!r}; expected raw or clean")
        self.schema = schema
        self.db_path = Path(db_path or os.environ.get("OLIST_DB", DEFAULT_DB))
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"database not found: {self.db_path} (build it with python -m pipeline)"
            )
        self._con = duckdb.connect(
            str(self.db_path),
            read_only=True,
            config={"enable_external_access": "false", "lock_configuration": "true"},
        )

    def close(self) -> None:
        self._con.close()

    def schema_text(self) -> str:
        """Tables and columns of the schema, one table per line, for the plan prompt."""
        rows = self._con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? ORDER BY table_name, ordinal_position",
            [self.schema],
        ).fetchall()
        tables: dict[str, list[str]] = {}
        for table, column, dtype in rows:
            tables.setdefault(table, []).append(f"{column} {dtype}")
        return "\n".join(f"{self.schema}.{t}({', '.join(cols)})" for t, cols in tables.items())

    def population(self, sql: str, label: str) -> Population:
        """Check a population SQL: the guard, a column order_id, and one cohort per order."""
        if self.schema != "clean":
            raise PopulationError("a population is only possible on the clean schema")
        checked = guard_population(sql, self.schema)
        columns = self._execute(f"SELECT * FROM ({checked}) AS population_rows LIMIT 0").columns
        if "order_id" not in columns:
            raise PopulationError("population_sql must return a column named order_id")
        pop = Population(sql=checked, label=label, with_cohort="cohort" in columns)
        if pop.with_cohort:
            probe = self._execute(
                f"{pop.with_clause()} SELECT order_id, count(*) AS n, count(cohort) AS named "
                "FROM population GROUP BY 1 HAVING n > 1 OR named < n LIMIT 1"
            )
            if probe.rows:
                row = probe.rows[0]
                if row["named"] < row["n"]:
                    raise PopulationError(f"order {row['order_id']} has no cohort; every order needs one")
                raise PopulationError(
                    f"order {row['order_id']} is in more than one cohort; give each order exactly one cohort"
                )
        return pop

    def query(self, sql: str, population: Population | None = None) -> Result:
        """Run one guarded SELECT; interrupt it after QUERY_TIMEOUT_S seconds."""
        return self._execute(guard(sql, self.schema, population))

    def execute_fixed(self, sql: str, params: list | None = None) -> Result:
        """SQL written in this codebase, never by Claude: it skips the guard.

        The connection is still read-only with external access off.
        """
        return self._execute(sql, params)

    def _execute(self, sql: str, params: list | None = None) -> Result:
        con = self._con.cursor()
        timer = threading.Timer(QUERY_TIMEOUT_S, con.interrupt)
        timer.start()
        try:
            cur = con.execute(sql, params or [])
            columns = [d[0] for d in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            timer.cancel()
            con.close()
        return Result(columns=columns, rows=rows, sql=sql)

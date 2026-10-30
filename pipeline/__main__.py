"""CLI: uv run python -m pipeline --zip data/olist.zip --db olist.duckdb"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import zipfile
from pathlib import Path

from pipeline.build_db import ReconciliationError, build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline", description="Build olist.duckdb from the Kaggle zip.")
    parser.add_argument("--zip", required=True, help="path to data/olist.zip")
    parser.add_argument("--db", required=True, help="path of the DuckDB file to write")
    parser.add_argument("--embeddings", default="data/review_embeddings.parquet",
                        help="parquet cache of review vectors (read if present, extended with new texts)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    zip_path = Path(args.zip)
    if not zip_path.is_file():
        print(f"zip not found: {zip_path}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="olist-raw-") as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp)
        try:
            build(tmp, args.db, embeddings_path=args.embeddings)
        except ReconciliationError as exc:
            print(f"reconciliation failed: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

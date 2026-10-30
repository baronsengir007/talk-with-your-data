"""The pipeline: raw CSVs in, one DuckDB file with the raw layer and the clean layer out."""

from pipeline.build_db import ReconciliationError, build

__all__ = ["build", "ReconciliationError"]

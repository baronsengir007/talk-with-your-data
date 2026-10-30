from collections.abc import Sequence
from pathlib import Path

import pytest

from pipeline.build_db import EMBEDDING_DIM

FIXTURES = Path(__file__).parent / "fixtures"


def fake_embed(texts: Sequence[str]) -> list[list[float]]:
    """Offline embedder: a unit vector whose position is the text length, so vectors differ per text."""
    vectors = []
    for text in texts:
        vector = [0.0] * EMBEDDING_DIM
        vector[len(text) % EMBEDDING_DIM] = 1.0
        vectors.append(vector)
    return vectors


@pytest.fixture
def fixture_csv_dir() -> Path:
    """Tiny raw layer: 3 orders, 3 customers, 2 reviews on one order, 3 payments on one order (two of them vouchers)."""
    return FIXTURES


@pytest.fixture(scope="session")
def fixture_db(tmp_path_factory) -> Path:
    """olist.duckdb built once from the tiny raw layer, for analyst tests."""
    from pipeline import build

    folder = tmp_path_factory.mktemp("db")
    db = folder / "olist.duckdb"
    build(FIXTURES, db, embeddings_path=folder / "review_embeddings.parquet", embed=fake_embed)
    return db

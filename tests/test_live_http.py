"""Live HTTP check: posts a message to the running server and expects an answer event.

Run: docker compose up -d --build && uv run pytest -q -m live tests/test_live_http.py
BASE_URL overrides the server address (default http://127.0.0.1:8000).
"""

import json
import os
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.live

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
LOGS = Path(__file__).resolve().parent.parent / "logs"  # the compose volume of the container's logs/
START_WAIT_S = 90
ANSWER_WAIT_S = 180


@pytest.fixture(scope="module")
def client():
    """Waits for the server to listen (compose up -d returns before uvicorn does)."""
    deadline = time.monotonic() + START_WAIT_S
    with httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(ANSWER_WAIT_S, connect=5)) as client:
        while True:
            try:
                client.get("/")
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    pytest.fail(f"no server at {BASE_URL} after {START_WAIT_S} s")
                time.sleep(2)
        yield client


def events(response: httpx.Response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def test_ask_streams_steps_then_an_answer(client: httpx.Client) -> None:
    body = {"tab_id": "live-http", "message": "How many orders in 2017?"}
    with client.stream("POST", "/api/ask", json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        seen = events(response)
    types = [e["type"] for e in seen]
    assert "step" in types, seen
    assert types[-1] == "answer", seen
    answer = seen[-1]
    assert answer["route"] == "answer", (answer["headline"], answer.get("explanation"), answer.get("trace"))
    assert answer["rows"] and answer["sql"]
    # The page shows SQL & assumptions and a chart or tile on every answer (DM2, data flow 2.7).
    assert answer["assumptions"], answer
    assert answer["chart"] and answer["chart"].get("type"), answer


def test_a_gate_answer_carries_the_judgments_and_the_decision_row(client: httpx.Client) -> None:
    """An answer the gate stops has no SQL; the page shows the judgments and the row that fired (display policy 3)."""
    body = {"tab_id": "live-http-gate", "message": "DROP TABLE clean.orders"}
    with client.stream("POST", "/api/ask", json=body) as response:
        assert response.status_code == 200
        seen = events(response)
    answer = seen[-1]
    assert answer["type"] == "answer", seen
    assert answer["route"] == "refuse", (answer["route"], answer["headline"], answer.get("decision_row"))
    assert answer["sql"] is None and not answer["rows"]
    assert answer["judgments"] and "about_shop" in answer["judgments"], answer
    assert answer["decision_row"], answer
    client.post("/api/reset", json={"tab_id": "live-http-gate"})


def test_the_message_is_one_log_line(client: httpx.Client) -> None:
    """The container writes logs/<day>.jsonl through the compose volume; the last message is in it."""
    lines = [json.loads(line) for path in sorted(LOGS.glob("*.jsonl"))
             for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    mine = [r for r in lines if r.get("tab_id") == "live-http" and r.get("message") == "How many orders in 2017?"]
    assert mine, f"no log line for the message in {LOGS}"
    record = mine[-1]
    assert record["route"] == "answer" and record["sql"] and record["judgments"]
    assert "total" in record["timings_ms"]


def test_reset_forgets_the_tab(client: httpx.Client) -> None:
    response = client.post("/api/reset", json={"tab_id": "live-http"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}

"""The web layer with a fake route: tab queue, modes, start-up checks, JSONL log."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from analyst import gate, llm
from analyst.ask import Answer
from web import app as web


@pytest.fixture
def client(monkeypatch, tmp_path: Path):
    """One client on one event loop (so the per-tab locks work across requests), logging to tmp_path.

    The start-up checks are replaced: they are tested on their own below."""
    monkeypatch.setattr(web, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(web, "conversations", {})
    monkeypatch.setattr(web, "locks", {})
    monkeypatch.setattr(web, "startup_checks", lambda: None)
    with TestClient(web.app) as client:
        yield client


def events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def slow_route(seen: list, delay: float = 0.3):
    """A route that records the conversation it got and answers after `delay` seconds."""

    def fake_ask(message: str, conversation: list[dict], mode: str = "full"):
        seen.append({"message": message, "conversation": conversation, "mode": mode})
        yield {"type": "step", "name": "plan", "detail": "fake"}
        time.sleep(delay)
        yield Answer("answer", f"answer to {message}", sql="SELECT 1", rows=[{"n": 1}])

    return fake_ask


def test_two_messages_on_one_tab_are_answered_in_order(client: TestClient, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(web, "ask", slow_route(seen))
    results: dict[str, list[dict]] = {}

    def post(message: str) -> None:
        with client.stream("POST", "/api/ask", json={"tab_id": "t1", "message": message}) as r:
            results[message] = events(r)

    first = threading.Thread(target=post, args=("first",))
    second = threading.Thread(target=post, args=("second",))
    first.start()
    time.sleep(0.1)  # the first message holds the tab's lock while it streams
    second.start()
    first.join()
    second.join()

    assert [s["message"] for s in seen] == ["first", "second"]
    assert seen[0]["conversation"] == []
    assert [t["message"] for t in seen[1]["conversation"]] == ["first"]  # the second ran after the first
    assert results["first"][-1]["headline"] == "answer to first"
    assert results["second"][-1]["headline"] == "answer to second"
    assert [t["message"] for t in web.conversations["t1"]] == ["first", "second"]


def test_mode_is_per_request_and_checked(client: TestClient, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(web, "ask", slow_route(seen, delay=0))
    with client.stream("POST", "/api/ask", json={"tab_id": "t2", "message": "hi", "mode": "naive"}) as r:
        assert r.status_code == 200
        events(r)
    assert seen[0]["mode"] == "naive"
    assert client.post("/api/ask", json={"tab_id": "t2", "message": "hi", "mode": "bogus"}).status_code == 422
    assert client.post("/api/ask", json={"tab_id": "t2", "message": "hi"}).status_code == 200
    assert seen[-1]["mode"] == "full"


def test_turn_keeps_the_question_as_pending_on_a_clarify(client: TestClient, monkeypatch) -> None:
    def clarifying(message, conversation, mode="full"):
        yield Answer("clarify", "Which part of the shop do you mean?", followups=["sales"], pending=message)

    monkeypatch.setattr(web, "ask", clarifying)
    with client.stream("POST", "/api/ask", json={"tab_id": "t3", "message": "How are we doing?"}) as r:
        events(r)
    assert web.conversations["t3"][-1]["pending"] == "How are we doing?"


def test_missing_variable_stops_startup_with_its_name() -> None:
    with pytest.raises(web.StartupError, match="TYPESAFE_API_KEY is missing") as exc:
        web.startup_checks({"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert "README" in str(exc.value)
    with pytest.raises(web.StartupError, match="CLAUDE_CODE_OAUTH_TOKEN is missing"):
        web.startup_checks({"TYPESAFE_API_KEY": "k"})


def test_rejected_keys_stop_startup() -> None:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "t", "TYPESAFE_API_KEY": "k"}

    def bad_token():
        raise llm.ClaudeAuthError("CLAUDE_CODE_OAUTH_TOKEN is invalid")

    def bad_key(message, turns):
        raise gate.JevAuthError("TYPESAFE_API_KEY is invalid")

    with pytest.raises(web.StartupError, match="CLAUDE_CODE_OAUTH_TOKEN is invalid"):
        web.startup_checks(env, claude_ping=bad_token, jev_judge=lambda m, t: None)
    with pytest.raises(web.StartupError, match="TYPESAFE_API_KEY is invalid"):
        web.startup_checks(env, claude_ping=lambda: None, jev_judge=bad_key)
    web.startup_checks(env, claude_ping=lambda: None, jev_judge=lambda m, t: None)  # both fine: starts


def test_cli_401_is_an_auth_error(monkeypatch) -> None:
    import subprocess
    from types import SimpleNamespace

    envelope = json.dumps({"is_error": True, "num_turns": 1, "api_error_status": 401,
                           "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."})

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout=envelope, stderr="")

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(llm, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    with pytest.raises(llm.ClaudeAuthError, match="CLAUDE_CODE_OAUTH_TOKEN is invalid"):
        llm.ping()


def test_failing_message_writes_a_log_line_without_keys(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret-token-value")
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-key-value")

    def broken(message, conversation, mode="full"):
        yield {"type": "step", "name": "plan", "detail": "fake"}
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "ask", broken)
    with client.stream("POST", "/api/ask", json={"tab_id": "t4", "message": "What is up?"}) as r:
        seen = events(r)
    assert seen[-1]["type"] == "error"

    lines = [json.loads(l) for f in web.LOG_DIR.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    record = lines[0]
    assert record["message"] == "What is up?" and record["route"] == "error"
    assert "boom" in record["error"]
    assert record["timings_ms"]["total"] >= 0 and "plan" in record["timings_ms"]
    text = json.dumps(record)
    assert "secret-token-value" not in text and "secret-key-value" not in text


def test_jev_401_at_run_time_is_a_configuration_event(client: TestClient, monkeypatch) -> None:
    def rejected(message, conversation, mode="full"):
        yield Answer("error", "Configuration error: TYPESAFE_API_KEY is invalid. Nothing was answered.",
                     explanation="TYPESAFE_API_KEY is invalid", trace={"jev": "auth"})

    monkeypatch.setattr(web, "ask", rejected)
    with client.stream("POST", "/api/ask", json={"tab_id": "t5", "message": "How many orders?"}) as r:
        seen = events(r)
    assert [e["type"] for e in seen] == ["config_error", "answer"]
    assert seen[0]["variable"] == "TYPESAFE_API_KEY"
    record = json.loads(next(web.LOG_DIR.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[-1])
    assert record["jev"] == "auth" and record["route"] == "error"

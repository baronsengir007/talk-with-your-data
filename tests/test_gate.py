"""The decision table (pure) and the Jev request/answer handling with a fake Jev."""

import httpx
import pytest

from analyst import gate
from analyst.gate import Judgments, JevAuthError, JevUnavailable, build_request, decide, judge


def j(**over) -> Judgments:
    base = dict(about_shop=0.95, asks_change=0.02, specific=0.9, needs_missing=0.05, asks_why=0.1,
                topic="sales", topic_probabilities={"sales": 0.7, "delivery": 0.2, "reviews": 0.06, "customers": 0.04})
    base.update(over)
    return Judgments(**base)


# Every row of the decision table, in order.
def test_row1_asks_change_refuses_before_anything_else() -> None:
    route = decide(j(asks_change=0.9, needs_missing=0.9, about_shop=0.1))
    assert route.name == "refuse" and route.row.startswith("1.")


def test_row2_missing_data() -> None:
    route = decide(j(needs_missing=0.8, about_shop=0.1))
    assert route.name == "missing_data" and route.row.startswith("2.")


def test_row3_not_about_the_shop() -> None:
    route = decide(j(about_shop=0.1))
    assert route.name == "not_understood" and route.row.startswith("3.")


def test_row4_not_specific_clarifies() -> None:
    route = decide(j(specific=0.1))
    assert route.name == "clarify" and route.row.startswith("4.")


@pytest.mark.parametrize("field", ["specific", "about_shop", "asks_change"])
def test_row4_unsure_band_clarifies(field: str) -> None:
    for p in (0.3, 0.5, 0.69):
        assert decide(j(**{field: p})).name == "clarify", (field, p)


def test_row4_is_skipped_after_a_clarify_reply() -> None:
    route = decide(j(specific=0.4), answered_clarify=True)
    assert route.name == "continue" and route.row.startswith("6.")


def test_row5_unsure_only_about_missing_data_continues_with_flag() -> None:
    route = decide(j(needs_missing=0.5))
    assert route.name == "continue" and route.may_be_missing and route.row.startswith("5.")


def test_row6_clear_question_continues() -> None:
    route = decide(j())
    assert route.name == "continue" and not route.may_be_missing and route.row.startswith("6.")


def test_thresholds_are_inclusive_at_yes_and_exclusive_at_no() -> None:
    assert decide(j(asks_change=0.7)).name == "refuse"
    assert decide(j(about_shop=0.29)).name == "not_understood"
    assert decide(j(about_shop=0.3)).name == "clarify"
    assert decide(j(specific=0.7)).name == "continue"


def test_topics_in_order_follow_the_probabilities() -> None:
    assert j().topics_in_order == ["sales", "delivery", "reviews", "customers"]


# judge(): request shape and answer parsing, with a fake HTTP call.
FAKE_BODY = {
    "model": "jev-latest",
    "answers": {
        "about_shop": {"type": "noul", "noul": 0.97},
        "asks_change": {"type": "noul", "noul": 0.01},
        "specific": {"type": "noul", "noul": 0.88},
        "needs_missing": {"type": "noul", "noul": 0.04},
        "asks_why": {"type": "noul", "noul": 0.03},
        "topic": {"type": "choice", "choice": "sales", "confidence": 0.8,
                  "probabilities": {"delivery": 0.1, "sales": 0.8, "reviews": 0.05, "customers": 0.05}},
    },
}


def test_judge_sends_five_nouls_and_a_choice_in_one_request(monkeypatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    sent = {}

    def fake_post(payload, api_key):
        sent["payload"], sent["key"] = payload, api_key
        return FAKE_BODY

    out = judge("How many orders in 2017?", [{"message": "hi", "summary": "hello"}], post=fake_post)
    assert sent["key"] == "test-key"
    questions = sent["payload"]["questions"]
    assert [q["type"] for q in questions.values()].count("noul") == 5
    assert questions["topic"]["type"] == "choice"
    assert sent["payload"]["state"]["message"] == "How many orders in 2017?"
    assert sent["payload"]["state"]["previous_turns"] == [{"user": "hi", "assistant": "hello"}]
    assert out.about_shop == 0.97 and out.topic == "sales"


def test_request_leaves_out_empty_turns() -> None:
    assert "previous_turns" not in build_request("x", [])["state"]


def test_missing_key_is_an_auth_error(monkeypatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(JevAuthError):
        judge("x", [])


def test_malformed_answer_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    with pytest.raises(JevUnavailable):
        judge("x", [], post=lambda payload, key: {"answers": {"about_shop": {"noul": "high"}}})


def test_network_error_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    def down(payload, key):
        raise httpx.ConnectError("no route")

    with pytest.raises(JevUnavailable):
        judge("x", [], post=down)


def test_http_401_is_an_auth_error(monkeypatch) -> None:
    def fake_http_post(url, json, headers, timeout):
        return httpx.Response(401, text="bad key", request=httpx.Request("POST", url))

    monkeypatch.setattr(gate.httpx, "post", fake_http_post)
    with pytest.raises(JevAuthError):
        gate._post({}, "k")


# judge_answer(): the correlation check on the rendered answer (U6).
def test_judge_answer_sends_one_noul_with_message_and_answer(monkeypatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    sent = {}

    def fake_post(payload, api_key):
        sent["payload"] = payload
        return {"answers": {"causal_claim": {"type": "noul", "noul": 0.82}}}

    p = gate.judge_answer("Why are late orders rated worse?", "Late orders score 2.1 because they are late.",
                          post=fake_post)
    assert p == 0.82
    assert list(sent["payload"]["questions"]) == ["causal_claim"]
    assert sent["payload"]["state"] == {"message": "Why are late orders rated worse?",
                                        "answer": "Late orders score 2.1 because they are late."}
    assert "previous_turns" not in sent["payload"]["state"]


def test_judge_answer_malformed_or_down_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    with pytest.raises(JevUnavailable):
        gate.judge_answer("x", "y", post=lambda payload, key: {"answers": {}})

    def down(payload, key):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(JevUnavailable):
        gate.judge_answer("x", "y", post=down)

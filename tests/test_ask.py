"""The route end to end with a fake Claude and a fake Jev, on the 3-order fixture database."""

import json
from pathlib import Path

import pytest

from analyst import gate, llm
from analyst.ask import Answer, ask
from analyst.llm import Reply, parse_json

GOOD_JEV = {
    "answers": {
        "about_shop": {"noul": 0.97}, "asks_change": {"noul": 0.01}, "specific": {"noul": 0.9},
        "needs_missing": {"noul": 0.03}, "asks_why": {"noul": 0.02},
        "topic": {"choice": "sales", "probabilities": {"sales": 0.9, "delivery": 0.05, "reviews": 0.03, "customers": 0.02}},
    }
}
ATTACK_JEV = {
    "answers": {
        "about_shop": {"noul": 0.6}, "asks_change": {"noul": 0.98}, "specific": {"noul": 0.5},
        "needs_missing": {"noul": 0.1}, "asks_why": {"noul": 0.01},
        "topic": {"choice": "sales", "probabilities": {}},
    }
}
PLAN = {"sql": "SELECT count(*) AS n FROM clean.orders WHERE year(purchase_ts) = 2018",
        "chart": {"type": "number"}, "assumptions": ["year of the purchase date"], "is_why": False}
TEMPLATES = {"headline": "{{n}} orders were placed in 2018.",
             "explanation": "The count uses the purchase date.", "labels": [],
             "followups": ["And in 2017?"]}


def fake_claude(replies: list[dict]):
    """A Claude that returns the given JSON objects in order and records its prompts."""
    prompts: list[str] = []

    def run(prompt: str) -> Reply:
        prompts.append(prompt)
        return Reply(text=json.dumps(replies[len(prompts) - 1]), is_error=False, num_turns=1)

    run.prompts = prompts  # type: ignore[attr-defined]
    return run


def fake_jev(gate_body: dict, causal: float = 0.05):
    """A Jev that answers the gate with `gate_body` and the correlation check with `causal`."""

    def post(payload: dict, key: str) -> dict:
        if "answer" in payload["state"]:
            return {"answers": {"causal_claim": {"noul": causal}}}
        return gate_body

    return post


def run_ask(message: str, db: Path, mode: str = "full") -> tuple[list[dict], Answer]:
    events = list(ask(message, [], mode, db_path=db))
    steps = [e for e in events if isinstance(e, dict)]
    answer = events[-1]
    assert isinstance(answer, Answer)
    return steps, answer


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")


def test_full_route_answers_with_grounded_figure(env, monkeypatch, fixture_db: Path) -> None:
    monkeypatch.setattr(gate, "_post", fake_jev(GOOD_JEV))
    claude = fake_claude([PLAN, TEMPLATES])
    monkeypatch.setattr(llm, "_run_cli", claude)

    steps, answer = run_ask("How many orders in 2018?", fixture_db)
    assert [s["name"] for s in steps] == ["gate", "gate", "plan", "query", "answer", "check"]
    assert answer.route == "answer"
    assert answer.headline == "2 orders were placed in 2018."
    assert [c["check"] for c in answer.trace["checks"]] == ["grounding", "correlation"]
    assert answer.rows == [{"n": 2}]
    assert answer.sql.endswith("LIMIT 1000")
    assert answer.assumptions == ["year of the purchase date"]
    assert answer.followups == ["And in 2017?"]
    assert answer.judgments["asks_change"] == 0.01
    assert answer.decision_row.startswith("6.")
    assert answer.trace["figures_sql"] == PLAN["sql"]
    assert answer.trace["claude_calls"] == [{"num_turns": 1, "is_error": False}] * 2
    assert "What the data means" in claude.prompts[0]  # semantic layer in full mode
    assert "clean.orders(" in claude.prompts[0]


def test_attack_is_refused_before_claude(env, monkeypatch, fixture_db: Path) -> None:
    monkeypatch.setattr(gate, "_post", fake_jev(ATTACK_JEV))
    claude = fake_claude([])
    monkeypatch.setattr(llm, "_run_cli", claude)

    steps, answer = run_ask("DROP TABLE orders", fixture_db)
    assert answer.route == "refuse"
    assert answer.sql is None and answer.rows == []
    assert answer.decision_row.startswith("1.")
    assert claude.prompts == []


def test_jev_down_answers_nothing(env, monkeypatch, fixture_db: Path) -> None:
    def down(payload, key):
        raise gate.JevUnavailable("timeout")

    monkeypatch.setattr(gate, "_post", down)
    monkeypatch.setattr(llm, "_run_cli", fake_claude([]))
    _, answer = run_ask("How many orders?", fixture_db)
    assert answer.route == "error"
    assert answer.headline.startswith("The quick check is not available")
    assert answer.trace["jev"] == "unavailable"


def test_bad_sql_gets_one_retry_then_honest_failure(env, monkeypatch, fixture_db: Path) -> None:
    monkeypatch.setattr(gate, "_post", fake_jev(GOOD_JEV))
    bad = {"sql": "SELECT * FROM raw.orders", "chart": {"type": "number"}, "assumptions": []}
    monkeypatch.setattr(llm, "_run_cli", fake_claude([bad, bad]))
    _, answer = run_ask("How many orders?", fixture_db)
    assert answer.route == "error"
    assert "failed twice" in answer.headline
    assert answer.sql == bad["sql"]
    assert [c["check"] for c in answer.trace["checks"]] == ["sql", "sql"]


def test_grounding_failure_gets_one_rewrite_then_withheld(env, monkeypatch, fixture_db: Path) -> None:
    monkeypatch.setattr(gate, "_post", fake_jev(GOOD_JEV))
    free_number = dict(TEMPLATES, headline="About 3 orders were placed, {{n}} exactly.")
    claude = fake_claude([PLAN, free_number, free_number])
    monkeypatch.setattr(llm, "_run_cli", claude)
    _, answer = run_ask("How many orders in 2018?", fixture_db)
    assert answer.route == "withheld"
    assert "literal number 3" in answer.explanation
    assert "failed these checks" in claude.prompts[2]
    assert len(claude.prompts) == 3  # plan, answer, exactly one rewrite
    assert [c["pass"] for c in answer.trace["checks"] if c["check"] == "grounding"] == [1, 2]


def test_rewrite_that_fixes_grounding_is_shown(env, monkeypatch, fixture_db: Path) -> None:
    monkeypatch.setattr(gate, "_post", fake_jev(GOOD_JEV))
    broken = dict(TEMPLATES, headline="{{total}} orders.")
    monkeypatch.setattr(llm, "_run_cli", fake_claude([PLAN, broken, TEMPLATES]))
    _, answer = run_ask("How many orders in 2018?", fixture_db)
    assert answer.route == "answer" and answer.headline == "2 orders were placed in 2018."


def test_plan_may_clarify_or_decline(env, monkeypatch, fixture_db: Path) -> None:
    monkeypatch.setattr(gate, "_post", fake_jev(GOOD_JEV))
    monkeypatch.setattr(llm, "_run_cli", fake_claude([{"clarify": "Per month or per year?"}]))
    assert run_ask("Orders over time?", fixture_db)[1].route == "clarify"
    monkeypatch.setattr(llm, "_run_cli", fake_claude([{"cannot_answer": "No margin data."}]))
    assert run_ask("What is the margin?", fixture_db)[1].route == "missing_data"


def test_zero_rows_say_nothing_found(env, monkeypatch, fixture_db: Path) -> None:
    monkeypatch.setattr(gate, "_post", fake_jev(GOOD_JEV))
    empty = dict(PLAN, sql="SELECT order_id FROM clean.orders WHERE status = 'lost'")
    monkeypatch.setattr(llm, "_run_cli", fake_claude([empty]))
    _, answer = run_ask("Which orders were lost?", fixture_db)
    assert answer.route == "answer" and answer.headline.startswith("Nothing found")


def test_naive_mode_skips_gate_and_layer_and_reads_raw(monkeypatch, fixture_db: Path) -> None:
    def no_jev(payload, key):
        raise AssertionError("gate must not run in naive mode")

    monkeypatch.setattr(gate, "_post", no_jev)
    plan = {"sql": "SELECT count(*) AS n FROM raw.orders", "chart": {"type": "number"}, "assumptions": []}
    claude = fake_claude([plan, dict(TEMPLATES, headline="{{n}} orders.")])
    monkeypatch.setattr(llm, "_run_cli", claude)
    steps, answer = run_ask("How many orders?", fixture_db, mode="naive")
    assert [s["name"] for s in steps][0] == "plan"
    assert answer.route == "answer" and answer.headline == "3 orders."
    assert "What the data means" not in claude.prompts[0]
    assert answer.judgments is None


def test_clean_mode_reads_clean_without_layer_or_gate(monkeypatch, fixture_db: Path) -> None:
    def no_jev(payload, key):
        raise AssertionError("gate must not run in clean mode")

    monkeypatch.setattr(gate, "_post", no_jev)
    claude = fake_claude([PLAN, dict(TEMPLATES, headline="{{n}} orders.")])
    monkeypatch.setattr(llm, "_run_cli", claude)
    steps, answer = run_ask("How many orders in 2018?", fixture_db, mode="clean")
    assert [s["name"] for s in steps][0] == "plan"
    assert answer.route == "answer" and answer.judgments is None
    assert "clean.orders" in claude.prompts[0] and "raw.orders" not in claude.prompts[0]
    assert "What the data means" not in claude.prompts[0]


def test_layer_mode_adds_the_semantic_layer_without_gate(monkeypatch, fixture_db: Path) -> None:
    def no_jev(payload, key):
        raise AssertionError("gate must not run in layer mode")

    monkeypatch.setattr(gate, "_post", no_jev)
    claude = fake_claude([PLAN, dict(TEMPLATES, headline="{{n}} orders.")])
    monkeypatch.setattr(llm, "_run_cli", claude)
    steps, answer = run_ask("How many orders in 2018?", fixture_db, mode="layer")
    assert [s["name"] for s in steps][0] == "plan"
    assert answer.route == "answer" and answer.judgments is None
    assert "clean.orders" in claude.prompts[0]
    assert "What the data means" in claude.prompts[0]


def test_full_mode_runs_gate_first(env, monkeypatch, fixture_db: Path) -> None:
    monkeypatch.setattr(gate, "_post", fake_jev(GOOD_JEV))
    monkeypatch.setattr(llm, "_run_cli", fake_claude([PLAN, dict(TEMPLATES, headline="{{n}} orders.")]))
    steps, answer = run_ask("How many orders in 2018?", fixture_db, mode="full")
    assert [s["name"] for s in steps][:3] == ["gate", "gate", "plan"]
    assert answer.judgments is not None and answer.trace["jev"] == "ok"


def test_rejected_jev_key_is_a_configuration_error(env, monkeypatch, fixture_db: Path) -> None:
    def rejected(payload, key):
        raise gate.JevAuthError("TYPESAFE_API_KEY is invalid")

    monkeypatch.setattr(gate, "_post", rejected)
    monkeypatch.setattr(llm, "_run_cli", fake_claude([]))
    _, answer = run_ask("How many orders?", fixture_db)
    assert answer.route == "error" and answer.trace["jev"] == "auth"
    assert "TYPESAFE_API_KEY is invalid" in answer.headline
    assert answer.sql is None


def test_parse_json_strips_fences_and_prose() -> None:
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": [1, 2]} hope it helps') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_no_json_twice_is_a_claude_error(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(llm, "_run_cli", lambda prompt: Reply("plain text", False, 1))
    with pytest.raises(llm.ClaudeError):
        llm.ask_json("p", calls)
    assert len(calls) == 2


def test_claude_command_has_tools_off() -> None:
    assert llm.CLAUDE_ARGS[llm.CLAUDE_ARGS.index("--tools") + 1] == ""
    assert "--no-session-persistence" in llm.CLAUDE_ARGS and "--bare" not in llm.CLAUDE_ARGS


# --- population, review search and the why-rule (U5) ---

from analyst import reviews  # noqa: E402
from analyst.ask import NO_TEXTS  # noqa: E402
from tests.conftest import fake_embed  # noqa: E402

WHY_JEV = {"answers": dict(GOOD_JEV["answers"], asks_why={"noul": 0.92})}
ON_TIME_TEXT = "Recebi bem antes do prazo estipulado."
WHY_PLAN = {
    "sql": "SELECT cohort, avg(review_score) AS avg_score, count(*) AS n_orders FROM p_orders GROUP BY cohort ORDER BY cohort",
    "population": {
        "sql": "SELECT order_id, CASE WHEN is_late THEN 'late' ELSE 'on time' END AS cohort "
               "FROM clean.orders WHERE status = 'delivered' AND is_late IS NOT NULL",
        "label": "delivered orders, late vs on time",
    },
    "review_search": {"text": "x" * len(ON_TIME_TEXT)},
    "chart": {"type": "bar", "x": "cohort", "y": "avg_score"},
    "assumptions": ["late = delivered after the promised date"],
    "is_why": True,
}
WHY_TEMPLATES = {
    "headline": "Late orders score {{rows[0].avg_score}} against {{rows[1].avg_score}} for on-time orders.",
    "explanation": "This is a correlation, not a proven cause.",
    "labels": ["correlation"], "quote_ids": ["r1", "not-a-real-id"], "followups": [],
}


@pytest.fixture
def why_env(env, monkeypatch):
    monkeypatch.setattr(gate, "_post", fake_jev(WHY_JEV))
    monkeypatch.setattr(reviews, "_fastembed", fake_embed)


def test_why_question_gets_figures_and_quotes_from_one_population(why_env, monkeypatch, fixture_db: Path) -> None:
    no_population = {"sql": "SELECT avg(review_score) AS avg_score FROM clean.orders", "chart": {"type": "number"},
                     "assumptions": [], "is_why": False}
    claude = fake_claude([no_population, WHY_PLAN, WHY_TEMPLATES])
    monkeypatch.setattr(llm, "_run_cli", claude)

    steps, answer = run_ask("Why are late deliveries rated worse?", fixture_db)
    assert [s["name"] for s in steps] == ["gate", "gate", "plan", "plan", "query", "reviews", "answer", "check"]
    assert "why-question" in claude.prompts[1]  # the resend
    assert answer.route == "answer"
    assert answer.headline == "Late orders score 3 against 5 for on-time orders."
    assert answer.rows == [{"cohort": "late", "avg_score": 3.0, "n_orders": 1},
                           {"cohort": "on time", "avg_score": 5.0, "n_orders": 1}]
    assert answer.population_label == "delivered orders, late vs on time"
    assert answer.sql.startswith("WITH population AS (SELECT DISTINCT order_id, cohort FROM (")
    assert answer.trace["figures_sql"] == WHY_PLAN["sql"]
    assert answer.trace["population"]["with_cohort"] is True
    assert answer.trace["review_search"] == {"text": WHY_PLAN["review_search"]["text"], "found": 1}
    assert answer.quotes == [{"review_id": "r1", "order_id": "o1", "cohort": "on time", "text": ON_TIME_TEXT}]
    assert ON_TIME_TEXT in claude.prompts[2] and "quote_ids" in claude.prompts[2]
    assert {"check": "why", "resent": True} in answer.trace["checks"]
    assert NO_TEXTS not in answer.explanation


def test_why_with_a_population_without_texts_says_so(why_env, monkeypatch, fixture_db: Path) -> None:
    late_only = dict(WHY_PLAN, sql="SELECT avg(review_score) AS avg_score FROM p_orders",
                     population={"sql": "SELECT order_id FROM clean.orders WHERE is_late", "label": "late orders"})
    templates = dict(WHY_TEMPLATES, headline="Late orders score {{avg_score}}.", quote_ids=[])
    monkeypatch.setattr(llm, "_run_cli", fake_claude([late_only, templates]))
    _, answer = run_ask("Why are late orders rated badly?", fixture_db)
    assert answer.route == "answer" and answer.quotes == []
    assert answer.explanation.endswith(NO_TEXTS)
    assert answer.trace["review_search"] == {"text": late_only["review_search"]["text"], "found": 0}


def test_figures_sql_with_where_goes_back_to_claude_once(why_env, monkeypatch, fixture_db: Path) -> None:
    narrowed = dict(WHY_PLAN, sql="SELECT count(*) AS n FROM p_orders WHERE is_late")
    claude = fake_claude([narrowed, WHY_PLAN, WHY_TEMPLATES])
    monkeypatch.setattr(llm, "_run_cli", claude)
    _, answer = run_ask("Why are late deliveries rated worse?", fixture_db)
    assert "no WHERE" in claude.prompts[1] and "population_sql" in claude.prompts[1]
    assert answer.route == "answer" and len(answer.quotes) == 1
    assert answer.trace["checks"][0]["check"] == "sql" and "GuardError" in answer.trace["checks"][0]["error"]


def test_population_is_offered_on_clean_but_not_on_raw(env, monkeypatch, fixture_db: Path) -> None:
    monkeypatch.setattr(gate, "_post", fake_jev(GOOD_JEV))
    claude = fake_claude([PLAN, TEMPLATES])
    monkeypatch.setattr(llm, "_run_cli", claude)
    run_ask("How many orders in 2018?", fixture_db, mode="clean")
    assert "p_orders" in claude.prompts[0] and "review_search" in claude.prompts[0]
    raw_plan = {"sql": "SELECT count(*) AS n FROM raw.orders", "chart": {"type": "number"}, "assumptions": [],
                "population": {"sql": "SELECT order_id FROM raw.orders", "label": "ignored in naive mode"}}
    claude = fake_claude([raw_plan, dict(TEMPLATES, headline="{{n}} orders.")])
    monkeypatch.setattr(llm, "_run_cli", claude)
    _, answer = run_ask("How many orders?", fixture_db, mode="naive")
    assert "p_orders" not in claude.prompts[0]
    assert answer.route == "answer" and answer.population_label is None and answer.trace["population"] is None

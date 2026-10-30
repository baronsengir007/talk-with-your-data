"""ask(message, conversation, mode): the route. Gate -> plan -> guard -> query -> reviews -> answer -> grounding.

A generator: it yields step events ({"type": "step", "name", "detail"})
while it works, then one Answer. The web layer and the eval runner read
only the Answer. Modes: naive (raw schema, no semantic layer, no gate, no
review search), clean (clean schema, no layer, no gate), layer (clean +
layer, no gate), full (layer + gate + checks).

Checks on the answer (data flow 2.6): grounding (code) and, in full mode,
the correlation check (Jev) run on the rendered text. On any failure
Claude gets one rewrite with every failure listed; both checks run again
on the final text; still failing -> route `withheld` with the SQL and rows.

Conversation (data flow 2.2, ADR 0006): a turn is what turn() returns:
message, summary, sql and `pending`. A clarifying question (from the
decision table or from the plan) stores the question as `pending` on its
own turn; only the last turn's `pending` counts, so the next message is
combined with it once and a later message is not.

Population: when the plan names a group of orders, the warehouse wraps it
into the p_* views; the figures SQL reads only those, and the review
search reads p_reviews of the same population, so figures and quotes
describe the same orders. In full mode a why-question must carry a
population and a review search; the plan is sent back once without them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from analyst import gate, grounding, llm, reviews
from analyst.warehouse import GuardError, Population, Warehouse

MODES = ("naive", "clean", "layer", "full")
LAST_TURNS = 3
WHY_THRESHOLD = 0.7
EXAMPLES = [
    "How many orders were placed in 2017?",
    "What share of delivered orders arrived late?",
    "Which product categories sell most?",
]
QUICK_CHECK_UNAVAILABLE = "The quick check is not available right now, please try again."
CONFIGURATION_ERROR = "Configuration error: TYPESAFE_API_KEY is invalid. Nothing was answered."
NO_TEXTS = "There are no customer texts for this group."
NO_SEARCH = "No customer texts were searched for this question."
LOOKING_UP = "Looking up the answer in the data"  # plan and query show as one step on the page


@dataclass
class Answer:
    route: str  # answer | refuse | missing_data | not_understood | clarify | withheld | error
    headline: str
    explanation: str = ""
    chart: dict | None = None
    rows: list[dict] = field(default_factory=list)
    sql: str | None = None
    population_label: str | None = None
    assumptions: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    quotes: list[dict] = field(default_factory=list)  # {review_id, order_id, cohort, text}, from the database
    followups: list[str] = field(default_factory=list)
    judgments: dict | None = None
    decision_row: str | None = None
    trace: dict = field(default_factory=dict)
    pending: str | None = None  # set on a clarify: the question the next message answers

    @property
    def summary(self) -> str:
        """Short text stored in the conversation as this turn's answer."""
        return self.headline


def turn(message: str, answer: Answer) -> dict:
    """What the conversation keeps of one exchange, in the shape ask() reads."""
    return {"message": message, "summary": answer.summary, "sql": answer.sql, "pending": answer.pending}


def _step(name: str, detail: str = "") -> dict:
    return {"type": "step", "name": name, "detail": detail}


def _search_text(plan: dict) -> str:
    search = plan.get("review_search") or {}
    return str(search.get("text", "") or "").strip() if isinstance(search, dict) else ""


def ask(message: str, conversation: list[dict], mode: str = "full", db_path=None) -> Iterator:
    """Run the route for one message. Yields step dicts, then one Answer."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    turns = [t for t in conversation if "message" in t][-LAST_TURNS:]
    pending = conversation[-1].get("pending") if conversation else None
    answered_clarify = pending is not None
    question = f"{pending}\nUser means: {message}" if pending else message
    trace: dict = {"mode": mode, "plan": None, "figures_sql": None, "final_sql": None,
                   "population": None, "review_search": None, "claude_calls": [], "checks": []}
    judgments = None
    decision_row = None
    may_be_missing = False
    jev_why = False

    if mode == "full":
        yield _step("gate", "Jev checks your question")
        try:
            j = gate.judge(question, turns)
        except gate.JevUnavailable as exc:
            yield _jev_unavailable(exc, trace)
            return
        judgments = j.as_dict()
        route = gate.decide(j, answered_clarify=answered_clarify)
        decision_row = route.row
        trace["jev"] = "ok"
        if route.name != "continue":
            yield _stopped_by_gate(route, j, judgments, trace, question)
            return
        yield _step("gate", "Question is clear")
        may_be_missing = route.may_be_missing
        jev_why = j.asks_why > WHY_THRESHOLD

    schema = "raw" if mode == "naive" else "clean"
    layer = llm.semantic_layer() if mode in ("layer", "full") else None
    warehouse = Warehouse(schema, db_path)
    try:
        yield _step("plan", LOOKING_UP)
        prompt = llm.plan_prompt(
            question, turns, warehouse.schema_text(), schema, layer, may_be_missing, answered_clarify
        )
        try:
            plan = llm.ask_json(prompt, trace["claude_calls"])
        except llm.ClaudeError as exc:
            yield Answer("error", "Claude did not answer.", explanation=str(exc), trace=trace)
            return
        trace["plan"] = plan
        if plan.get("clarify"):
            yield Answer("clarify", str(plan["clarify"]), judgments=judgments,
                         decision_row=decision_row, trace=trace, pending=question)
            return
        if plan.get("cannot_answer"):
            yield Answer("missing_data", str(plan["cannot_answer"]), judgments=judgments,
                         decision_row=decision_row, trace=trace)
            return

        # Why-rule (full mode): numbers and customer texts from one population, or the plan goes back once.
        is_why = mode == "full" and (jev_why or bool(plan.get("is_why")))
        if is_why and not (plan.get("population") and _search_text(plan)):
            trace["checks"].append({"check": "why", "resent": True})
            yield _step("plan", "A why-question needs customer reviews too, so I am planning again")
            retry_prompt = llm.plan_prompt(
                question, turns, warehouse.schema_text(), schema, layer,
                may_be_missing, answered_clarify, why_missing=True,
            )
            try:
                plan = llm.ask_json(retry_prompt, trace["claude_calls"])
            except llm.ClaudeError as exc:
                yield Answer("error", "Claude did not answer.", explanation=str(exc), trace=trace)
                return
            trace["plan"] = plan
            if plan.get("clarify"):
                yield Answer("clarify", str(plan["clarify"]), judgments=judgments,
                             decision_row=decision_row, trace=trace, pending=question)
                return
            if plan.get("cannot_answer"):
                yield Answer("missing_data", str(plan["cannot_answer"]), judgments=judgments,
                             decision_row=decision_row, trace=trace)
                return

        sql = str(plan.get("sql", "")).strip()
        trace["figures_sql"] = sql
        assumptions = [str(a) for a in plan.get("assumptions", [])]

        yield _step("query", LOOKING_UP)
        result = None
        population: Population | None = None
        error = None
        for attempt in range(2):
            try:
                population = _population(warehouse, plan, schema)
                trace["population"] = None if population is None else {
                    "sql": population.sql, "label": population.label, "with_cohort": population.with_cohort}
                result = warehouse.query(sql, population)
                break
            except (GuardError, Exception) as exc:  # DuckDB errors have many classes
                error = f"{type(exc).__name__}: {exc}"
                trace["checks"].append({"check": "sql", "attempt": attempt + 1, "error": error})
                if attempt == 1:
                    break
                yield _step("plan", "The lookup failed, so I am trying once more")
                retry_prompt = llm.plan_prompt(
                    question, turns, warehouse.schema_text(), schema, layer,
                    may_be_missing, answered_clarify, error=error,
                )
                try:
                    plan = llm.ask_json(retry_prompt, trace["claude_calls"])
                except llm.ClaudeError as exc:
                    yield Answer("error", "Claude did not answer.", explanation=str(exc), trace=trace)
                    return
                trace["plan"] = plan
                sql = str(plan.get("sql", "")).strip()
                trace["figures_sql"] = sql
                assumptions = [str(a) for a in plan.get("assumptions", [])]
        if result is None:
            yield Answer("error", "I could not answer this: the query failed twice.",
                         explanation=error or "", sql=sql, assumptions=assumptions,
                         judgments=judgments, decision_row=decision_row, trace=trace)
            return
        trace["final_sql"] = result.sql
        label = population.label if population else None
        chart = plan.get("chart") or {"type": "number"}
        if not result.rows:
            yield Answer("answer", "Nothing found: the query returned no rows.", chart=chart,
                         rows=[], sql=result.sql, population_label=label, assumptions=assumptions,
                         judgments=judgments, decision_row=decision_row, trace=trace)
            return

        found: list[reviews.Review] = []
        search_text = _search_text(plan)
        if population is not None and search_text:
            yield _step("reviews", "Reading customer reviews")
            found = reviews.search(warehouse, population, search_text)
            trace["review_search"] = {"text": search_text, "found": len(found)}
        note = ""
        if is_why and not found:
            note = NO_TEXTS if population is not None else NO_SEARCH
            trace["checks"].append({"check": "why", "texts": 0, "note": note})

        yield _step("answer", "Writing the answer")
        prompt = llm.answer_prompt(question, plan, result.columns, result.rows, found)
        try:
            templates = llm.ask_json(prompt, trace["claude_calls"])
        except llm.ClaudeError as exc:
            yield Answer("error", "Claude did not answer.", explanation=str(exc), sql=result.sql,
                         rows=result.rows, population_label=label, assumptions=assumptions, trace=trace)
            return

        with_result = dict(sql=result.sql, rows=result.rows, population_label=label, assumptions=assumptions)
        yield _step("check", "Checking the answer against the data")
        headline, explanation, failures = _render(templates, result.rows, message)
        trace["checks"].append({"check": "grounding", "pass": 1, "failures": failures})
        try:
            failures += _correlation(mode, question, f"{headline} {explanation}", trace, 1)
        except gate.JevUnavailable as exc:
            yield _jev_unavailable(exc, trace, **with_result)
            return
        if failures:
            yield _step("answer", "A check failed, so I am rewriting the answer")
            try:
                templates = llm.ask_json(llm.rewrite_prompt(prompt, failures), trace["claude_calls"])
            except llm.ClaudeError as exc:
                yield Answer("error", "Claude did not answer.", explanation=str(exc), trace=trace, **with_result)
                return
            yield _step("check", "Checking the new answer")
            headline, explanation, failures = _render(templates, result.rows, message)
            trace["checks"].append({"check": "grounding", "pass": 2, "failures": failures})
            try:
                failures += _correlation(mode, question, f"{headline} {explanation}", trace, 2)
            except gate.JevUnavailable as exc:
                yield _jev_unavailable(exc, trace, **with_result)
                return
        if failures:
            yield Answer("withheld", "I could not verify this answer.",
                         explanation="; ".join(failures), chart=chart,
                         judgments=judgments, decision_row=decision_row, trace=trace, **with_result)
            return

        if note:
            explanation = f"{explanation} {note}".strip()
        yield Answer(
            "answer", headline, explanation=explanation, chart=chart, rows=result.rows,
            sql=result.sql, population_label=label, assumptions=assumptions,
            labels=[str(x) for x in templates.get("labels", [])],
            quotes=_quotes(found, templates.get("quote_ids")),
            followups=[str(x) for x in templates.get("followups", [])],
            judgments=judgments, decision_row=decision_row, trace=trace,
        )
    finally:
        warehouse.close()


def _population(warehouse: Warehouse, plan: dict, schema: str) -> Population | None:
    """The plan's population, checked by the warehouse; None without one (or in naive mode)."""
    spec = plan.get("population")
    if not spec or schema != "clean":
        return None
    if not isinstance(spec, dict) or not str(spec.get("sql", "")).strip():
        raise GuardError('population must be {"sql": "<one SELECT on clean returning order_id>", "label": "..."}')
    return warehouse.population(str(spec["sql"]).strip(), str(spec.get("label", "") or ""))


def _quotes(found: list[reviews.Review], quote_ids) -> list[dict]:
    """Quotes are database rows chosen by id; Claude's text never becomes a quote."""
    if not found:
        return []
    wanted = {str(i) for i in quote_ids} if isinstance(quote_ids, list) else set()
    chosen = [r for r in found if r.review_id in wanted] or found
    return [r.as_quote() for r in chosen]


def _correlation(mode: str, question: str, text: str, trace: dict, pass_no: int) -> list[str]:
    """The correlation check (full mode only): Jev reads the rendered text; at or above YES it fails."""
    if mode != "full":
        return []
    p = gate.judge_answer(question, text)
    failed = p >= gate.YES
    trace["checks"].append({"check": "correlation", "pass": pass_no, "causal_claim": p, "failed": failed})
    if failed:
        return [f"the text presents a correlation as a proven cause (quick check {p:.2f}); "
                "call it a correlation, a pattern or an interpretation, not a cause"]
    return []


def _jev_unavailable(exc: Exception, trace: dict, **with_result) -> Answer:
    """Jev is down (or rejects the key): nothing is answered unchecked; the SQL and rows stay visible.

    A rejected key is a configuration error: trace["jev"] = "auth", so the web layer can show a banner."""
    if isinstance(exc, gate.JevAuthError):
        trace["jev"] = "auth"
        return Answer("error", CONFIGURATION_ERROR, explanation=str(exc), trace=trace, **with_result)
    trace["jev"] = "unavailable"
    return Answer("error", QUICK_CHECK_UNAVAILABLE, explanation=str(exc), trace=trace, **with_result)


def _render(templates: dict, rows: list[dict], question: str) -> tuple[str, str, list[str]]:
    head = grounding.render(str(templates.get("headline", "")), rows, question)
    body = grounding.render(str(templates.get("explanation", "")), rows, question)
    return head.text, body.text, head.failures + body.failures


def _stopped_by_gate(route: gate.Route, j: gate.Judgments, judgments: dict, trace: dict, question: str) -> Answer:
    """Answers without data: one short sentence; the folded receipt shows what was judged and why it stopped."""
    common = dict(judgments=judgments, decision_row=route.row, trace=trace)
    if route.name == "refuse":
        return Answer("refuse", "I only read the shop data. I cannot change data or my instructions.", **common)
    if route.name == "missing_data":
        text, instead = gate.MISSING[j.missing]
        return Answer("missing_data", text, followups=[instead] if instead else [], **common)
    if route.name == "not_understood":
        return Answer("not_understood", "I did not understand that as a question about the shop data. "
                      "Try one of these:", followups=EXAMPLES, **common)
    return Answer("clarify", "Which part of the shop do you mean?",
                  followups=j.topics_in_order, pending=question, **common)

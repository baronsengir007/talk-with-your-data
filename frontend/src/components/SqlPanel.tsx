import type { Answer, Judgments } from "../types";
import { Table } from "./Chart";

const JUDGMENT_NAMES: [keyof Judgments, string][] = [
  ["about_shop", "about the shop data"],
  ["asks_change", "asks to change data or instructions"],
  ["specific", "specific enough"],
  ["needs_missing", "needs data the dataset does not have"],
  ["asks_why", "asks why"],
];

const ROUTE_WORDS: Record<string, string> = {
  answer: "answered from the data",
  refuse: "refused",
  missing_data: "the data cannot answer it",
  not_understood: "not understood",
  clarify: "asked back",
  withheld: "withheld after the checks",
  error: "failed",
};

/** The receipt under every card: the SQL and assumptions behind an answer, or the judgments behind a stop. */
export default function SqlPanel({ answer }: { answer: Answer }) {
  const hasQuery = !!answer.sql;
  const gate = !hasQuery && !!answer.judgments;
  if (!hasQuery && !gate) return null;
  return (
    <details className="receipt">
      <summary>{hasQuery ? "SQL & assumptions" : "Why it stopped: the judgments"}</summary>
      {/* An answer shows only what a reader can check: assumptions, scope, SQL, result. Jev's judgments
          explain a stop, so they show only when no query ran. */}
      {hasQuery ? <QueryPart answer={answer} /> : <p className="no-query">No query was run.</p>}
      {!hasQuery && answer.judgments && <JudgmentsPart judgments={answer.judgments} row={answer.decision_row} />}
      {!hasQuery && <TracePart answer={answer} />}
    </details>
  );
}

function QueryPart({ answer }: { answer: Answer }) {
  return (
    <>
      {answer.assumptions.length > 0 && (
        <section className="receipt-block">
          <h4>Assumptions</h4>
          <ul className="assumptions">
            {answer.assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </section>
      )}
      {answer.population_label && (
        <section className="receipt-block">
          <h4>Orders in scope</h4>
          <p>{answer.population_label}</p>
        </section>
      )}
      <section className="receipt-block">
        <h4>SQL</h4>
        <pre className="sql">
          <code>{answer.sql}</code>
        </pre>
      </section>
      {answer.rows.length > 0 && (
        <section className="receipt-block">
          <h4>Result</h4>
          <Table rows={answer.rows} />
        </section>
      )}
    </>
  );
}

function JudgmentsPart({ judgments, row }: { judgments: Judgments; row: string | null }) {
  const topics = Object.entries(judgments.topic_probabilities ?? {}).sort((a, b) => b[1] - a[1]);
  return (
    <section className="receipt-block">
      <h4>Quick check (Jev)</h4>
      <ul className="judgments">
        {JUDGMENT_NAMES.map(([key, label]) => {
          const p = judgments[key];
          const value = typeof p === "number" ? p : 0;
          return (
            <li key={key}>
              <span className="judgment-label">{label}</span>
              <span className="meter" aria-hidden="true">
                <span className="meter-fill" style={{ width: `${Math.round(value * 100)}%` }} />
              </span>
              <span className="judgment-value">{value.toFixed(2)}</span>
            </li>
          );
        })}
      </ul>
      <p className="topics">
        Topic: {judgments.topic}
        {topics.length > 0 && ` (${topics.map(([t, p]) => `${t} ${p.toFixed(2)}`).join(", ")})`}
      </p>
      {row && (
        <p className="decision-row">
          Decision table: <code>{row}</code>
        </p>
      )}
    </section>
  );
}

function TracePart({ answer }: { answer: Answer }) {
  const calls = answer.trace.claude_calls;
  const checks = answer.trace.checks;
  const jev = answer.trace.jev;
  const parts: string[] = [`Route: ${ROUTE_WORDS[answer.route] ?? answer.route}.`];
  if (Array.isArray(calls)) parts.push(`Claude calls: ${calls.length}.`);
  if (typeof jev === "string") parts.push(`Quick check: ${jev}.`);
  if (Array.isArray(checks) && checks.length > 0) {
    const failed = checks.filter((c) => {
      if (!c || typeof c !== "object") return false;
      const check = c as { failed?: boolean; failures?: unknown[]; error?: string };
      return check.failed === true || (Array.isArray(check.failures) && check.failures.length > 0) || !!check.error;
    }).length;
    parts.push(failed ? `Checks: ${failed} of ${checks.length} failed.` : `Checks: ${checks.length} passed.`);
  }
  return <p className="trace">{parts.join(" ")}</p>;
}

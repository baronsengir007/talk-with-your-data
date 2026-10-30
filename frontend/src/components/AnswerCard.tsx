import type { Answer, Quote } from "../types";
import Chart from "./Chart";
import SqlPanel from "./SqlPanel";

/** The answer card: headline, explanation, chart, the receipt right under it, quotes, follow-ups. */
export default function AnswerCard({ answer, onFollowup }: { answer: Answer; onFollowup: (text: string) => void }) {
  const stopped = answer.route !== "answer";
  const buttons = answer.followups.filter((f) => f && f.trim());
  const buttonKind = answer.route === "clarify" ? "topic" : answer.route === "not_understood" ? "example" : "followup";
  return (
    <article className={`card route-${answer.route}`}>
      <h2 className="headline">{answer.headline}</h2>
      {answer.explanation && <p className="explanation">{answer.explanation}</p>}
      {!stopped && <Chart chart={answer.chart} rows={answer.rows} />}
      <SqlPanel answer={answer} />
      {!stopped && <Quotes quotes={answer.quotes} population={answer.population_label} />}
      {buttons.length > 0 && (
        <section className="followups-block">
          {buttonKind === "followup" && <h3>Possible follow-up questions</h3>}
          <div className={`followups followups-${buttonKind}`}>
            {buttons.map((f) => (
              <button type="button" key={f} className="chip" onClick={() => onFollowup(f)}>
                {f}
              </button>
            ))}
          </div>
        </section>
      )}
    </article>
  );
}

/** Customer voices, taken from the database by id, grouped by cohort under the population label. */
function Quotes({ quotes, population }: { quotes: Quote[]; population: string | null }) {
  if (quotes.length === 0) return null;
  const groups = new Map<string, Quote[]>();
  for (const q of quotes) {
    const key = q.cohort ?? "";
    groups.set(key, [...(groups.get(key) ?? []), q]);
  }
  return (
    <section className="quotes">
      <h3>What customers wrote{population ? `: ${population}` : ""}</h3>
      {[...groups.entries()].map(([cohort, items]) => (
        <div className="cohort" key={cohort}>
          {cohort && <h4>{cohort.replace(/_/g, " ")}</h4>}
          {items.map((q) => (
            <blockquote key={q.review_id}>
              <p>{q.text}</p>
              <footer>review {q.review_id.slice(0, 8)}, order {q.order_id.slice(0, 8)}</footer>
            </blockquote>
          ))}
        </div>
      ))}
    </section>
  );
}

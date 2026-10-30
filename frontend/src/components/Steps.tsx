import type { Step } from "../types";

/** What the bot is doing, in plain words, live while it works; folded into one line once the answer is in. */
export default function Steps({ steps, running }: { steps: Step[]; running: boolean }) {
  // Steps that say the same thing (plan and query) show as one line.
  const lines = steps
    .map((s) => s.detail || s.name)
    .filter((text, i, all) => i === 0 || text !== all[i - 1]);
  if (lines.length === 0) {
    return running ? <p className="steps-waiting">Starting...</p> : null;
  }
  const list = (
    <ol className="steps">
      {lines.map((text, i) => (
        <li key={i} className={running && i === lines.length - 1 ? "step current" : "step"}>
          {text}
        </li>
      ))}
    </ol>
  );
  if (running) return list;
  return (
    <details className="steps-done">
      <summary>How I got this answer</summary>
      {list}
    </details>
  );
}

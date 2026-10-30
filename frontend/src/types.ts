// The shapes the page reads from the server events (web/app.py: step, answer, error, config_error).

export type Step = { name: string; detail: string };

export type Row = Record<string, unknown>;

export type ChartSpec = { type: string; x?: string; y?: string };

export type Quote = { review_id: string; order_id: string; cohort: string | null; text: string };

export type Judgments = {
  about_shop: number;
  asks_change: number;
  specific: number;
  needs_missing: number;
  asks_why: number;
  topic: string;
  topic_probabilities: Record<string, number>;
};

export type Route = "answer" | "refuse" | "missing_data" | "not_understood" | "clarify" | "withheld" | "error";

/** The Answer dataclass of analyst/ask.py, as the answer event carries it. */
export type Answer = {
  route: Route;
  headline: string;
  explanation: string;
  chart: ChartSpec | null;
  rows: Row[];
  sql: string | null;
  population_label: string | null;
  assumptions: string[];
  labels: string[];
  quotes: Quote[];
  followups: string[];
  judgments: Judgments | null;
  decision_row: string | null;
  trace: Record<string, unknown>;
  pending: string | null;
};

/** One message and what the page knows of its answer so far. */
export type Turn = {
  id: number;
  message: string;
  status: "queued" | "running" | "done";
  steps: Step[];
  answer: Answer | null;
  error: { headline: string; detail: string } | null;
};

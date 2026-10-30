import { FormEvent, useEffect, useRef, useState } from "react";
import { ask, newTabId, reset, tabId } from "./api";
import AnswerCard from "./components/AnswerCard";
import Steps from "./components/Steps";
import type { Turn } from "./types";

const EXAMPLES = [
  "How many orders were placed in 2017?",
  "What share of delivered orders arrived late?",
  "Which product categories sell most?",
  "Why do late orders get worse reviews?",
];

let nextId = 1;

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [message, setMessage] = useState("");
  const [banner, setBanner] = useState<string | null>(null);
  const tab = useRef(tabId());
  const queue = useRef<Turn[]>([]);
  const working = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [turns]);

  function patch(id: number, change: (t: Turn) => Turn) {
    setTurns((ts) => ts.map((t) => (t.id === id ? change(t) : t)));
  }

  /** Runs the queued messages one after the other; the input stays open meanwhile. */
  async function pump() {
    if (working.current) return;
    working.current = true;
    while (queue.current.length > 0) {
      const turn = queue.current.shift()!;
      const owner = tab.current;
      patch(turn.id, (t) => ({ ...t, status: "running" }));
      try {
        await ask(owner, turn.message, (ev) => {
          if (ev.type === "step") patch(turn.id, (t) => ({ ...t, steps: [...t.steps, { name: ev.name, detail: ev.detail }] }));
          else if (ev.type === "answer") patch(turn.id, (t) => ({ ...t, answer: ev }));
          else if (ev.type === "error") patch(turn.id, (t) => ({ ...t, error: { headline: ev.headline, detail: ev.detail } }));
          else if (ev.type === "config_error") setBanner(ev.headline);
        });
      } catch (err) {
        patch(turn.id, (t) => ({ ...t, error: { headline: "The server could not be reached.", detail: String(err) } }));
      }
      patch(turn.id, (t) => ({ ...t, status: "done" }));
    }
    working.current = false;
  }

  function submit(text: string) {
    const clean = text.trim();
    if (!clean) return;
    const turn: Turn = { id: nextId++, message: clean, status: "queued", steps: [], answer: null, error: null };
    setTurns((ts) => [...ts, turn]);
    queue.current.push(turn);
    void pump();
  }

  function send(e: FormEvent) {
    e.preventDefault();
    submit(message);
    setMessage("");
    input.current?.focus();
  }

  async function newConversation() {
    const old = tab.current;
    queue.current = [];
    tab.current = newTabId();
    setTurns([]);
    setBanner(null);
    await reset(old);
    input.current?.focus();
  }

  const waiting = turns.filter((t) => t.status === "queued").length;

  return (
    <div className="page">
      <header className="top">
        <span className="brand">Talk With Your Data</span>
        <button type="button" className="ghost" onClick={newConversation}>
          New conversation
        </button>
      </header>
      {banner && <div className="banner" role="alert">{banner}</div>}
      <main className="thread">
        {turns.length === 0 ? (
          <section className="welcome">
            <h1>Hi, I'm your data analyst for the Olist webshop.</h1>
            <ul className="welcome-points">
              <li>Ask me anything about orders, deliveries, reviews and customers from 2016 to 2018.</li>
              <li>With every answer I show how I got it: the SQL and my assumptions, so you can check it.</li>
              <li>Ask me why something happens, and I show the figures and what customers wrote.</li>
              <li>When the data cannot answer, I say so.</li>
            </ul>
            <div className="followups followups-example">
              {EXAMPLES.map((q) => (
                <button type="button" key={q} className="chip" onClick={() => submit(q)}>
                  {q}
                </button>
              ))}
            </div>
          </section>
        ) : (
          turns.map((t) => (
            <section className="turn" key={t.id}>
              <p className="message">{t.message}</p>
              {t.status === "queued" ? (
                <p className="steps-waiting">Waiting for the previous answer...</p>
              ) : (
                <Steps steps={t.steps} running={t.status === "running" && !t.answer && !t.error} />
              )}
              {t.answer && <AnswerCard answer={t.answer} onFollowup={submit} />}
              {t.error && (
                <article className="card route-error">
                  <h2 className="headline">{t.error.headline}</h2>
                  <p className="explanation">{t.error.detail}</p>
                </article>
              )}
            </section>
          ))
        )}
        <div ref={bottom} className="thread-end" />
      </main>
      <form className="composer" onSubmit={send}>
        <div className="composer-row">
          <input
            ref={input}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Ask about orders, delivery, reviews, customers or sales"
            aria-label="Your question"
            autoFocus
          />
          <button type="submit" className="primary" disabled={!message.trim()}>
            Ask
          </button>
        </div>
        {waiting > 0 && <p className="queue-note">{waiting === 1 ? "1 question waiting" : `${waiting} questions waiting`}</p>}
      </form>
    </div>
  );
}

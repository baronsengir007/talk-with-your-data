"""FastAPI: /api/ask (SSE), /api/reset, static files, start-up checks, JSONL log.

The page posts {tab_id, message, mode?} to /api/ask and reads server-sent events:
`data: {"type": "step", ...}` while the route works, then one
`data: {"type": "answer", ...}` with the Answer's fields, or
`data: {"type": "error", ...}` when the route itself fails. A rejected Jev key
adds `data: {"type": "config_error", ...}` before the answer, for the banner.
Every message runs the same route with the tab's earlier turns (ADR 0006);
a tab handles one message at a time, a second one waits for the first
(ARCHITECTURE data flow 4); /api/reset forgets a tab. Every message is one
JSONL line in logs/<day>.jsonl, also when it fails (data flow 5). At start-up
the server checks .env and makes one tiny Claude call and one tiny Jev request
(Failure and recovery). The built React page in frontend/dist is served at /.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from analyst import gate, llm
from analyst.ask import MODES, Answer, ask, turn

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "frontend" / "dist"
LOG_DIR = Path(os.environ.get("LOG_DIR", ROOT / "logs"))
REQUIRED_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "TYPESAFE_API_KEY")
README_STEP = "README, step 'Create .env'"
PING_MESSAGE = "How many orders were placed in 2017?"

conversations: dict[str, list[dict]] = {}
locks: dict[str, asyncio.Lock] = {}


class StartupError(RuntimeError):
    """The server must not start: a missing variable or a rejected key."""


def startup_checks(environ=None, claude_ping=None, jev_judge=None) -> None:
    """Refuse to start on a missing .env variable or a key that Claude or Jev rejects."""
    env = os.environ if environ is None else environ
    for name in REQUIRED_VARS:
        if not env.get(name):
            raise StartupError(f"{name} is missing from .env; see the {README_STEP}.")
    try:
        (claude_ping or llm.ping)()
    except llm.ClaudeAuthError as exc:
        raise StartupError(f"{exc}; see the {README_STEP}.") from exc
    except llm.ClaudeError as exc:
        raise StartupError(f"The Claude CLI did not answer at start-up: {exc}") from exc
    try:
        (jev_judge or gate.judge)(PING_MESSAGE, [])
    except gate.JevAuthError as exc:
        raise StartupError(f"{exc}; see the {README_STEP}.") from exc
    except gate.JevUnavailable as exc:
        raise StartupError(f"Jev did not answer at start-up: {exc}") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await run_in_threadpool(startup_checks)
    except StartupError as exc:
        print(f"Cannot start: {exc}", file=sys.stderr, flush=True)
        raise
    yield


app = FastAPI(title="Talk With Your Data", lifespan=lifespan)


class AskRequest(BaseModel):
    tab_id: str
    message: str
    mode: str = "full"


class ResetRequest(BaseModel):
    tab_id: str


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str, ensure_ascii=False)}\n\n"


def log_message(record: dict) -> Path:
    """Append one JSONL line for one message. Only named fields go in, never a key value."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{datetime.now(timezone.utc).date().isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    return path


def _record(tab_id: str, mode: str, message: str, timings: dict, answer: Answer | None, error: str | None) -> dict:
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "tab_id": tab_id,
        "mode": mode,
        "message": message,
        "timings_ms": timings,
    }
    if answer is not None:
        record.update(
            route=answer.route,
            judgments=answer.judgments,
            decision_row=answer.decision_row,
            jev=answer.trace.get("jev"),
            sql=answer.sql,
            rows=len(answer.rows),
            claude_calls=answer.trace.get("claude_calls"),
            checks=answer.trace.get("checks"),
            answer=answer.headline if answer.route != "error" else None,
            error=f"{answer.headline} {answer.explanation}".strip() if answer.route == "error" else None,
        )
    else:
        record.update(route="error", error=error)
    return record


@app.post("/api/ask")
async def api_ask(req: AskRequest) -> StreamingResponse:
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is empty")
    if req.mode not in MODES:
        raise HTTPException(status_code=422, detail=f"mode must be one of {', '.join(MODES)}")
    conversation = conversations.setdefault(req.tab_id, [])
    lock = locks.setdefault(req.tab_id, asyncio.Lock())

    async def stream():
        started = time.monotonic()
        timings: dict[str, int] = {}
        answer: Answer | None = None
        error: str | None = None
        async with lock:  # one message per tab at a time; a second one waits here
            timings["queued"] = int((time.monotonic() - started) * 1000)
            events = ask(message, list(conversation), req.mode)
            end = object()
            try:
                while True:
                    event = await run_in_threadpool(next, events, end)
                    if event is end:
                        break
                    if isinstance(event, Answer):
                        answer = event
                        conversation.append(turn(message, event))
                        if event.trace.get("jev") == "auth":
                            yield _event({"type": "config_error", "variable": "TYPESAFE_API_KEY",
                                          "headline": event.headline})
                        yield _event({"type": "answer", **event.__dict__})
                    else:
                        timings[event.get("name", "step")] = int((time.monotonic() - started) * 1000)
                        yield _event(event)
            except Exception as exc:  # the route failed outside its own error path
                error = f"{type(exc).__name__}: {exc}"
                yield _event({"type": "error", "headline": "Something went wrong, please try again.",
                              "detail": error})
            finally:
                timings["total"] = int((time.monotonic() - started) * 1000)
                try:
                    log_message(_record(req.tab_id, req.mode, message, timings, answer, error))
                except OSError as exc:
                    print(f"could not write the log: {exc}", file=sys.stderr, flush=True)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/reset")
async def api_reset(req: ResetRequest) -> dict:
    conversations.pop(req.tab_id, None)
    return {"ok": True}


if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="page")
else:

    @app.get("/", response_class=PlainTextResponse)
    async def no_page() -> str:
        return "The page is not built. Run npm run build in frontend/, or use POST /api/ask."

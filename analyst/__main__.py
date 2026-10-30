"""CLI: uv run python -m analyst "How many orders in 2017?" [--mode full] [--db olist.duckdb]"""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

from analyst.ask import MODES, Answer, ask


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analyst", description="Ask the analyst one question.")
    parser.add_argument("message")
    parser.add_argument("--mode", choices=MODES, default="full")
    parser.add_argument("--db", default=None, help="DuckDB file (default: $OLIST_DB or olist.duckdb)")
    parser.add_argument("--json", action="store_true", help="print the whole Answer as JSON")
    args = parser.parse_args(argv)
    load_dotenv()

    answer: Answer | None = None
    for event in ask(args.message, [], args.mode, db_path=args.db):
        if isinstance(event, Answer):
            answer = event
        else:
            print(f"[{event['name']}] {event['detail']}", file=sys.stderr)
    assert answer is not None
    if args.json:
        print(json.dumps(answer.__dict__, default=str, indent=2, ensure_ascii=False))
    else:
        print(f"route: {answer.route}")
        print(answer.headline)
        if answer.explanation:
            print(answer.explanation)
        if answer.sql:
            print(f"\nSQL:\n{answer.sql}")
        if answer.assumptions:
            print("assumptions: " + "; ".join(answer.assumptions))
        if answer.followups:
            print("follow-ups: " + " | ".join(answer.followups))
    return 0 if answer.route != "error" else 1


if __name__ == "__main__":
    sys.exit(main())

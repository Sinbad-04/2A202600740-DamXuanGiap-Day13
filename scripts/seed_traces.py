"""Seed Langfuse with traces in-process and flush before exit.

Running the agent in-process (instead of via the server) guarantees the
Langfuse client flushes every trace before the program exits, so none are
lost. Each trace now contains nested spans: run -> retrieve -> generate.

Usage:
    python scripts/seed_traces.py            # 12 traces
    python scripts/seed_traces.py 20         # custom count
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root on path

from dotenv import load_dotenv  # noqa: E402

load_dotenv()  # load LANGFUSE_* keys from .env

from app.agent import LabAgent  # noqa: E402
from app.tracing import langfuse_context, tracing_enabled  # noqa: E402

QUERIES = Path("data/sample_queries.jsonl")


def main() -> None:
    if not tracing_enabled():
        raise SystemExit("Tracing disabled: set LANGFUSE_PUBLIC_KEY/SECRET_KEY in .env first.")

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    queries = [json.loads(l) for l in QUERIES.read_text(encoding="utf-8").splitlines() if l.strip()]

    agent = LabAgent()
    sent = 0
    while sent < n:
        q = queries[sent % len(queries)]
        agent.run(user_id=q["user_id"], feature=q["feature"], session_id=q["session_id"], message=q["message"])
        sent += 1

    langfuse_context.flush()  # ensure all traces reach Langfuse before exit
    print(f"Seeded {sent} traces (each with run -> retrieve -> generate spans). Flushed.")


if __name__ == "__main__":
    main()

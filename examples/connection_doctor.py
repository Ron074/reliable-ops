"""connection_doctor.py -- a small unattended maintenance job built from reliable_ops.

Scenario: a product keeps many third-party account connections alive (think
"user linked their store account"). A listener appends status events to
data/connection_status.jsonl. Every few minutes this job:

  1. takes a single-instance lock (a hung earlier run can't block later runs forever),
  2. reads only the new lines of the status feed and lists connections broken right now,
     ignoring any that synced successfully after their bad status,
  3. asks an LLM to triage each one into a fixed set of next actions, through the
     Batch API with a live fallback so a slow batch never stalls the job,
  4. writes a triage report atomically, plus a run-status file a monitor can alert on.

Offline by default (fake LLM, no key, no cost):

    python examples/connection_doctor.py

--live uses the Anthropic SDK (pip install anthropic; ANTHROPIC_API_KEY set). The
live path is a sketch: it has not been run against the real API in this repo.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reliable_ops.batch_fallback import run_with_fallback  # noqa: E402
from reliable_ops.fakes import FakeLLMClient  # noqa: E402
from reliable_ops.single_run import (Deadline, LockHeld, SingleInstanceLock,  # noqa: E402
                                     atomic_write_text, write_run_status)
from reliable_ops.status_feed import HealthCheck, StatusFeed  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
MODEL = "claude-opus-5-5"
ACTIONS = ("ask_user_to_reconnect", "retry_later", "provider_outage", "needs_engineer")
BROKEN = {"auth_expired", "mfa_required", "rate_limited", "provider_error", "unknown_error"}

PROMPT = """You triage broken third-party account connections.
Connection: {key}
Latest status: {reason}
Answer with exactly one of: {actions}"""


def triage_params(key: str, reason: str) -> dict:
    return {
        "model": MODEL,
        "max_tokens": 1024,
        "output_config": {"effort": "low"},     # short classification: low effort is plenty
        "messages": [{"role": "user", "content": PROMPT.format(
            key=key, reason=reason, actions=", ".join(ACTIONS))}],
    }


def first_text(message) -> str:
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    return ""


def parse_action(text: str) -> str:
    """Accept only a known action; anything else is escalated, never guessed."""
    words = text.lower().replace(".", " ").split()
    return next((w for w in words if w in ACTIONS), "needs_engineer")


def fake_triage(params: dict) -> str:
    text = params["messages"][0]["content"]
    if "auth_expired" in text or "mfa_required" in text:
        return "ask_user_to_reconnect"
    if "rate_limited" in text:
        return "retry_later"
    if "provider_error" in text:
        return "provider_outage"
    return "needs_engineer"


def make_client(live: bool):
    if not live:
        # A slow batch: two of the requests finish before the deadline, the rest go live.
        return FakeLLMClient(fake_triage, ends_after_polls=None, finished={"acct-0007", "acct-0031"}), None
    import anthropic                                # optional dependency
    client = anthropic.Anthropic()                  # SDK retries kept for batch create/poll
    # run_with_fallback owns live retries; a short classification never needs the
    # SDK's 10-minute default timeout, and a hung call must not eat the run's deadline.
    live = client.with_options(max_retries=0, timeout=60.0)

    def live_call(params):
        # Server-side refusal fallback is accepted on live calls but not in batches,
        # so it is added here rather than in params.
        return live.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"],
                                         fallbacks="default")
    return client, live_call


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the real Anthropic API (costs money)")
    ap.add_argument("--batch-wait", type=float, default=None, help="seconds to wait for the batch")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    started = time.time()
    status_path = os.path.join(DATA, "run_status.json")
    deadline = Deadline(15 * 60)
    try:
        lock = SingleInstanceLock(os.path.join(DATA, "connection_doctor.lock"),
                                  stale_after_s=20 * 60).acquire()
    except LockHeld as exc:
        logging.warning("another run is active: %s", exc)
        return 0
    try:
        feed = StatusFeed(os.path.join(DATA, "connection_status.jsonl"))
        health = HealthCheck(feed, broken_codes=BROKEN, healthy_codes={"ok"}, fail_open=False)
        last_sync = json.load(open(os.path.join(DATA, "last_successful_sync.json"), encoding="utf-8"))
        problems = {k: r for k, r in health.all_problems().items()
                    if health.problem(k, last_success_at=last_sync.get(k)) is not None}
        if "*" in problems:
            raise RuntimeError(problems["*"])
        logging.info("%d broken connection(s): %s", len(problems), ", ".join(problems) or "none")

        client, live_call = make_client(args.live)
        wait = deadline.budget(args.batch_wait if args.batch_wait is not None
                               else (300 if args.live else 90))   # timer fires every 10 min
        # After a cancel, wait at most 2 more minutes, and only what the run's deadline has left.
        grace = min(120.0, max(0.0, deadline.remaining() - wait))
        clock_kw = {} if args.live else _instant_clock()
        report = run_with_fallback(client, {k: triage_params(k, r) for k, r in problems.items()},
                                   deadline_s=wait, poll_s=30, cancel_grace_s=grace,
                                   live_call=live_call, **clock_kw)
        lock.heartbeat()

        lines = ["# Connection triage", "", f"Run at {time.strftime('%Y-%m-%d %H:%M:%S')}. {report.summary()}.", "",
                 "| connection | status | next action | answered by |", "|---|---|---|---|"]
        for key, outcome in report.outcomes.items():
            action = parse_action(first_text(outcome.message)) if outcome.source != "failed" else "needs_engineer"
            lines.append(f"| {key} | {problems[key]} | {action} | {outcome.source} |")
        atomic_write_text(os.path.join(DATA, "triage_report.md"), "\n".join(lines) + "\n")
        write_run_status(status_path, ok=True, started=started, detail=report.summary())
        print("\n".join(lines))
        return 0
    except Exception as exc:
        write_run_status(status_path, ok=False, started=started, detail=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        lock.release()


def _instant_clock() -> dict:
    """Offline demo: simulate the batch wait instead of really sleeping."""
    from reliable_ops.fakes import FakeClock
    clock = FakeClock()
    return {"sleep": clock.sleep, "clock": clock}


if __name__ == "__main__":
    sys.exit(main())

"""Run many LLM requests through a discounted asynchronous batch API without
letting a slow batch hold up the caller.

The pattern
-----------
1. Submit every request as one batch (batch pricing is typically about half of
   live pricing).
2. Poll until the batch ends or the caller's deadline passes. Batches usually
   finish in minutes but can take hours on a busy day, so the caller always
   states how long it is willing to wait.
3. Past the deadline, cancel the batch. A cancelled batch still reports the
   requests that had already finished, so those answers are kept.
4. Re-send only what is missing (unfinished, errored, expired, or refused) as
   ordinary live calls with the *same* parameters, so every answer is the same
   kind of answer no matter which path produced it.

If the batch endpoint itself is down, everything goes live. Nothing is dropped
silently: every request ends up as "batch", "live" or "failed" with a reason.

Client interface
----------------
The client is duck-typed on the Anthropic Python SDK surface, so production
code passes ``anthropic.Anthropic()`` and tests pass a fake::

    client.messages.batches.create(requests=[{"custom_id": ..., "params": ...}])
    client.messages.batches.retrieve(batch_id).processing_status
        # "in_progress" | "canceling" | "ended"
    client.messages.batches.cancel(batch_id)
    client.messages.batches.results(batch_id)
        # iterable; each item has .custom_id and .result.type in
        # {"succeeded", "errored", "canceled", "expired"}; on success
        # .result.message is the Message
    client.messages.create(**params)

Some request options are only valid on live calls (for example server-side
refusal fallbacks or streaming). Keep those out of ``params`` and pass your own
``live_call`` that adds them.

Live retries
------------
Only errors that can succeed on a second try are retried: network failures,
timeouts, 408/409/429 and 5xx. A 400 (bad request), 401/403 (key or permission)
or 404 (wrong model name) fails the same way every time, so it is reported at
once instead of being sent again. The Anthropic SDK also retries on its own
(``max_retries``, default 2); give the live call a client with
``max_retries=0`` so the two retry loops don't multiply.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

# The Message Batches API accepts custom_id values of 1-64 letters, digits, "_" or "-".
_CUSTOM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# HTTP statuses worth a second try: timeout, conflict, rate limit (5xx handled below).
_RETRY_STATUS = {408, 409, 429}


def is_retryable(exc: BaseException) -> bool:
    """True if sending the same request again could succeed.

    Duck-typed so this module doesn't import the SDK: an error carrying an HTTP
    ``status_code`` is judged by that code; one without a status is retried only
    if it is a network-level failure (the SDK's ``APIConnectionError`` /
    ``APITimeoutError``, or Python's ``ConnectionError`` / ``TimeoutError``).
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in _RETRY_STATUS or status >= 500
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return any(c.__name__ in ("APIConnectionError", "APITimeoutError") for c in type(exc).__mro__)


@dataclass
class Outcome:
    """What happened to one request."""

    source: str                      # "batch" | "live" | "failed"
    message: Any = None              # the model's Message when source != "failed"
    error: str | None = None         # last error text when source == "failed"
    live_attempts: int = 0           # how many live calls were made for it
    notes: list[str] = field(default_factory=list)  # why it left the batch path, if it did


@dataclass
class RunReport:
    outcomes: dict[str, Outcome]
    batch_id: str | None
    batch_state: str                 # "ended" | "cancelled" | "stuck" | "unavailable" | "empty"

    def count(self, source: str) -> int:
        return sum(1 for o in self.outcomes.values() if o.source == source)

    def summary(self) -> str:
        return (f"batch {self.batch_id or '-'} ({self.batch_state}): "
                f"{self.count('batch')} from batch, {self.count('live')} live, "
                f"{self.count('failed')} failed")


def run_with_fallback(
    client: Any,
    requests: Mapping[str, dict],
    *,
    deadline_s: float,
    poll_s: float = 30.0,
    cancel_grace_s: float = 600.0,
    live_call: Callable[[dict], Any] | None = None,
    live_attempts: int = 2,
    retry_delay_s: float = 2.0,
    retry_refusals: bool = True,
    retryable: Callable[[BaseException], bool] = is_retryable,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RunReport:
    """Answer every request in ``requests`` ({custom_id: params}).

    deadline_s      how long to wait for the batch before cancelling it
    cancel_grace_s  how long to wait, after cancelling, for the batch to report "ended"
    live_call       makes one live call from params (default: client.messages.create)
    live_attempts   live tries per request before it is marked "failed"
    retry_refusals  re-send batch answers that stopped with stop_reason "refusal"
                    (a live call may carry a fallback model the batch can't)
    retryable       decides whether a failed live call is worth another try
                    (default: network errors, 408/409/429, 5xx)
    sleep, clock    injectable so tests run instantly
    """
    for cid in requests:
        if not _CUSTOM_ID.match(cid):
            raise ValueError(f"custom_id {cid!r} must be 1-64 chars of [A-Za-z0-9_-]")
    if deadline_s < 0 or cancel_grace_s < 0 or live_attempts < 1:
        raise ValueError("deadline_s and cancel_grace_s must be >= 0, live_attempts >= 1")

    pending: dict[str, dict] = dict(requests)
    outcomes: dict[str, Outcome] = {}
    notes: dict[str, list[str]] = {cid: [] for cid in requests}
    batch_id: str | None = None
    state = "empty"

    if pending:
        try:
            batch = client.messages.batches.create(
                requests=[{"custom_id": cid, "params": p} for cid, p in pending.items()])
            batch_id = batch.id
            log.info("batch %s: %d requests submitted", batch_id, len(pending))

            status = _wait_until_ended(client, batch_id, batch.processing_status,
                                       clock() + deadline_s, poll_s, sleep, clock)
            state = "ended"
            if status != "ended":
                log.warning("batch %s not done after %.0fs: cancelling; unfinished requests go live",
                            batch_id, deadline_s)
                client.messages.batches.cancel(batch_id)
                status = _wait_until_ended(client, batch_id, "canceling",
                                           clock() + cancel_grace_s, poll_s, sleep, clock)
                state = "cancelled" if status == "ended" else "stuck"

            if status == "ended":
                _collect(client, batch_id, pending, outcomes, notes, retry_refusals)
            else:
                log.warning("batch %s still not ended %.0fs after cancel; all of it goes live",
                            batch_id, cancel_grace_s)
        except Exception as exc:  # endpoint down, bad request, network: degrade, don't crash
            state = "unavailable"
            log.warning("batch path failed (%s: %s); %d requests go live",
                        type(exc).__name__, str(exc)[:200], len(pending))
            for cid in pending:
                notes[cid].append(f"batch unavailable: {type(exc).__name__}")

    call = live_call or (lambda params: client.messages.create(**params))
    for cid, params in pending.items():
        outcomes[cid] = _call_live(call, params, live_attempts, retry_delay_s, retryable, sleep)
        outcomes[cid].notes = notes[cid] or [f"not answered by batch ({state})"]

    report = RunReport({cid: outcomes[cid] for cid in requests}, batch_id, state)
    log.info(report.summary())
    return report


def _wait_until_ended(client, batch_id, status, until, poll_s, sleep, clock) -> str:
    while status != "ended" and clock() < until:
        sleep(max(0.0, min(poll_s, until - clock())))
        status = client.messages.batches.retrieve(batch_id).processing_status
    return status


def _collect(client, batch_id, pending, outcomes, notes, retry_refusals) -> None:
    """Move finished answers out of ``pending``. Results arrive in any order, so
    everything is keyed by custom_id, never by position."""
    for item in client.messages.batches.results(batch_id):
        cid = item.custom_id
        if cid not in pending:
            log.warning("batch %s returned unknown custom_id %r; ignored", batch_id, cid)
            continue
        kind = item.result.type
        if kind != "succeeded":
            notes[cid].append(f"batch result {kind}")
            continue
        message = item.result.message
        if retry_refusals and getattr(message, "stop_reason", None) == "refusal":
            notes[cid].append("batch answer was a refusal")
            continue
        outcomes[cid] = Outcome("batch", message=message)
        del pending[cid]


def _call_live(call, params, attempts, retry_delay_s, retryable, sleep) -> Outcome:
    for n in range(1, attempts + 1):
        try:
            return Outcome("live", message=call(params), live_attempts=n)
        except Exception as exc:
            if n == attempts or not retryable(exc):
                return Outcome("failed", error=f"{type(exc).__name__}: {str(exc)[:300]}",
                               live_attempts=n)
            sleep(retry_delay_s * 2 ** (n - 1))
    raise AssertionError("unreachable: attempts >= 1 is checked by the caller")

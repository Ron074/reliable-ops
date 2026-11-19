"""In-memory stand-ins for an LLM client and a clock, used by the tests and the
offline examples. No network, no API key, no cost."""
from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Callable


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self, start: float = 1_000.0):
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def text_message(text: str, stop_reason: str = "end_turn") -> NS:
    """Shaped like an SDK Message: .content is a list of blocks with .type/.text."""
    return NS(content=[NS(type="text", text=text)], stop_reason=stop_reason)


class FakeBatches:
    """Simulates the batch lifecycle.

    ends_after_polls   the batch reports "ended" on this poll (None = never on its own)
    ends_on_cancel     after cancel(), the next poll reports "ended"
    finished           custom_ids that finish (others come back "canceled" after a cancel,
                       or are absent if the batch never ends)
    result_types       per-id override of result.type ("errored", "expired", ...)
    refusals           ids whose batch answer has stop_reason "refusal"
    create_error       raise this from create() (batch endpoint down)
    """

    def __init__(self, answer: Callable[[dict], str], *, ends_after_polls=1, ends_on_cancel=True,
                 finished=None, result_types=None, refusals=(), create_error=None):
        self.answer = answer
        self.ends_after_polls = ends_after_polls
        self.ends_on_cancel = ends_on_cancel
        self.finished = finished
        self.result_types = dict(result_types or {})
        self.refusals = set(refusals)
        self.create_error = create_error
        self.polls = 0
        self.cancelled = False
        self.requests: dict[str, dict] = {}

    def create(self, requests):
        if self.create_error:
            raise self.create_error
        self.requests = {r["custom_id"]: r["params"] for r in requests}
        return NS(id="batch_fake_1", processing_status="in_progress")

    def retrieve(self, batch_id):
        self.polls += 1
        done = (self.ends_after_polls is not None and self.polls >= self.ends_after_polls) \
            or (self.cancelled and self.ends_on_cancel)
        status = "ended" if done else ("canceling" if self.cancelled else "in_progress")
        return NS(id=batch_id, processing_status=status)

    def cancel(self, batch_id):
        self.cancelled = True
        return NS(id=batch_id, processing_status="canceling")

    def results(self, batch_id):
        out = []
        for cid, params in reversed(list(self.requests.items())):   # any order, like the real API
            finished = self.finished is None or cid in self.finished
            kind = self.result_types.get(cid, "succeeded" if finished else "canceled")
            msg = None
            if kind == "succeeded":
                msg = text_message(self.answer(params),
                                   "refusal" if cid in self.refusals else "end_turn")
            out.append(NS(custom_id=cid, result=NS(type=kind, message=msg)))
        return out


class FakeMessages:
    def __init__(self, answer: Callable[[dict], str], batches: FakeBatches, fail_first=0):
        self.answer = answer
        self.batches = batches
        self.fail_first = fail_first          # first N live calls raise (transient error)
        self.live_calls: list[dict] = []

    def create(self, **params):
        self.live_calls.append(params)
        if len(self.live_calls) <= self.fail_first:
            raise ConnectionError("simulated transient network error")
        return text_message(self.answer(params))


class FakeLLMClient:
    """client.messages.create / client.messages.batches.* like the Anthropic SDK."""

    def __init__(self, answer: Callable[[dict], str] = lambda p: "ok", *, fail_first_live=0, **batch_kw):
        self.messages = FakeMessages(answer, FakeBatches(answer, **batch_kw), fail_first_live)

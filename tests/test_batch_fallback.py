import unittest

from reliable_ops.batch_fallback import run_with_fallback
from reliable_ops.fakes import FakeClock, FakeLLMClient


def params(i):
    return {"model": "claude-opus-5-5", "max_tokens": 64,
            "messages": [{"role": "user", "content": f"q{i}"}]}


REQS = {f"r{i}": params(i) for i in range(4)}
echo = lambda p: "answer to " + p["messages"][0]["content"]


def run(client, clock, **kw):
    kw.setdefault("deadline_s", 300)
    kw.setdefault("poll_s", 30)
    return run_with_fallback(client, REQS, sleep=clock.sleep, clock=clock, **kw)


class BatchFallbackTests(unittest.TestCase):
    def test_batch_finishes_in_time_no_live_calls(self):
        clock, client = FakeClock(), FakeLLMClient(echo, ends_after_polls=2)
        rep = run(client, clock)
        self.assertEqual(rep.batch_state, "ended")
        self.assertEqual(rep.count("batch"), 4)
        self.assertEqual(client.messages.live_calls, [])
        self.assertEqual(rep.outcomes["r2"].message.content[0].text, "answer to q2")
        self.assertEqual(list(rep.outcomes), list(REQS))       # caller's order, not the API's

    def test_deadline_cancels_keeps_finished_and_sends_rest_live(self):
        clock = FakeClock()
        client = FakeLLMClient(echo, ends_after_polls=None, finished={"r0", "r3"})
        rep = run(client, clock, deadline_s=120)
        self.assertTrue(client.messages.batches.cancelled)
        self.assertEqual(rep.batch_state, "cancelled")
        self.assertEqual({k for k, o in rep.outcomes.items() if o.source == "batch"}, {"r0", "r3"})
        self.assertEqual({k for k, o in rep.outcomes.items() if o.source == "live"}, {"r1", "r2"})
        # live calls use exactly the same params the batch would have used
        self.assertEqual(client.messages.live_calls, [REQS["r1"], REQS["r2"]])
        self.assertIn("batch result canceled", rep.outcomes["r1"].notes)

    def test_waits_no_longer_than_deadline(self):
        clock = FakeClock()
        client = FakeLLMClient(echo, ends_after_polls=None, ends_on_cancel=True)
        start = clock()
        run(client, clock, deadline_s=100, poll_s=30)
        # 30+30+30+10 before the cancel, then one poll after it
        self.assertLessEqual(clock() - start, 100 + 30)

    def test_batch_never_ends_even_after_cancel(self):
        clock = FakeClock()
        client = FakeLLMClient(echo, ends_after_polls=None, ends_on_cancel=False)
        rep = run(client, clock, deadline_s=60, cancel_grace_s=60)
        self.assertEqual(rep.batch_state, "stuck")
        self.assertEqual(rep.count("live"), 4)

    def test_batch_endpoint_down_everything_goes_live(self):
        clock = FakeClock()
        client = FakeLLMClient(echo, create_error=RuntimeError("503 overloaded"))
        rep = run(client, clock)
        self.assertEqual(rep.batch_state, "unavailable")
        self.assertEqual(rep.count("live"), 4)
        self.assertIn("batch unavailable: RuntimeError", rep.outcomes["r0"].notes)

    def test_errored_expired_and_refused_results_are_resent_live(self):
        clock = FakeClock()
        client = FakeLLMClient(echo, result_types={"r0": "errored", "r1": "expired"}, refusals={"r2"})
        rep = run(client, clock)
        self.assertEqual(rep.outcomes["r3"].source, "batch")
        for cid in ("r0", "r1", "r2"):
            self.assertEqual(rep.outcomes[cid].source, "live", cid)
        self.assertIn("batch answer was a refusal", rep.outcomes["r2"].notes)

    def test_refusal_kept_when_retry_disabled(self):
        clock = FakeClock()
        client = FakeLLMClient(echo, refusals={"r2"})
        rep = run(client, clock, retry_refusals=False)
        self.assertEqual(rep.outcomes["r2"].source, "batch")
        self.assertEqual(rep.outcomes["r2"].message.stop_reason, "refusal")

    def test_transient_live_error_is_retried_with_backoff(self):
        clock = FakeClock()
        client = FakeLLMClient(echo, create_error=RuntimeError("down"), fail_first_live=1)
        rep = run(client, clock, retry_delay_s=2)
        self.assertEqual(rep.outcomes["r0"].source, "live")
        self.assertEqual(rep.outcomes["r0"].live_attempts, 2)
        self.assertIn(2, clock.slept)

    def test_live_failure_is_reported_not_raised(self):
        clock = FakeClock()
        client = FakeLLMClient(echo, create_error=RuntimeError("down"), fail_first_live=99)
        rep = run(client, clock, live_attempts=3)
        self.assertEqual(rep.count("failed"), 4)
        self.assertTrue(rep.outcomes["r0"].error.startswith("ConnectionError"))
        self.assertEqual(rep.outcomes["r0"].live_attempts, 3)

    def test_permanent_errors_fail_at_once_without_retry(self):
        class StatusError(Exception):               # shaped like the SDK's APIStatusError
            def __init__(self, status_code):
                super().__init__(f"HTTP {status_code}")
                self.status_code = status_code

        for status in (400, 401, 403, 404):
            clock, calls = FakeClock(), []

            def bad_request(p, status=status):
                calls.append(p)
                raise StatusError(status)

            client = FakeLLMClient(echo, create_error=RuntimeError("down"))
            rep = run(client, clock, live_call=bad_request, live_attempts=3)
            self.assertEqual(rep.count("failed"), 4, status)
            self.assertEqual(len(calls), 4, status)          # one try each, no retries
            self.assertEqual(rep.outcomes["r0"].live_attempts, 1)
            self.assertEqual(clock.slept, [], status)        # no backoff waits either

    def test_retryable_classification(self):
        from reliable_ops.batch_fallback import is_retryable

        def status(code):
            e = Exception()
            e.status_code = code
            return e

        class APITimeoutError(Exception):           # matched by name, like the SDK class
            pass

        for exc in (status(408), status(409), status(429), status(500), status(529),
                    ConnectionError(), TimeoutError(), APITimeoutError()):
            self.assertTrue(is_retryable(exc), repr(exc))
        for exc in (status(400), status(401), status(404), status(413), ValueError(), KeyError()):
            self.assertFalse(is_retryable(exc), repr(exc))

    def test_rate_limit_is_retried_then_succeeds(self):
        clock, calls = FakeClock(), []

        def flaky(p):
            calls.append(p)
            if len(calls) == 1:
                e = Exception("429 rate limited")
                e.status_code = 429
                raise e
            return "ok"

        client = FakeLLMClient(echo, create_error=RuntimeError("down"))
        rep = run(client, clock, live_call=flaky, retry_delay_s=2)
        self.assertEqual(rep.count("live"), 4)
        self.assertEqual(rep.outcomes["r0"].live_attempts, 2)
        self.assertEqual(clock.slept, [2])

    def test_custom_live_call_is_used(self):
        clock = FakeClock()
        client = FakeLLMClient(echo, ends_after_polls=None, finished=set())
        seen = []
        rep = run(client, clock, deadline_s=30, live_call=lambda p: seen.append(p) or "custom")
        self.assertEqual(len(seen), 4)
        self.assertEqual(rep.outcomes["r0"].message, "custom")

    def test_bad_custom_id_rejected_before_any_call(self):
        client = FakeLLMClient(echo)
        with self.assertRaises(ValueError):
            run_with_fallback(client, {"has space": params(0)}, deadline_s=10)
        self.assertEqual(client.messages.batches.requests, {})

    def test_empty_input(self):
        rep = run_with_fallback(FakeLLMClient(echo), {}, deadline_s=10)
        self.assertEqual((rep.batch_state, rep.outcomes), ("empty", {}))


if __name__ == "__main__":
    unittest.main()

# reliable-ops

[![tests](https://github.com/Ron074/reliable-ops/actions/workflows/tests.yml/badge.svg)](https://github.com/Ron074/reliable-ops/actions/workflows/tests.yml)

Small, tested building blocks for automation that runs unattended: jobs on a timer,
services that call third-party APIs and LLMs, and the deploys that update them.
Standard library only, 50 tests, all offline: no API key, no cost.

```
python examples/connection_doctor.py       # the pieces working together, offline
python -m unittest discover -v            # 50 tests, about 15 s (deploy tests need bash)
```

## Example: keeping third-party account connections healthy

[`examples/connection_doctor.py`](examples/connection_doctor.py) is a maintenance job
for a product that keeps many users' third-party accounts linked (think "user connected
their store account"). It runs on a timer with nobody watching:

1. Takes a single-instance lock, so a hung earlier run can't block later runs forever.
2. Reads only the new lines of a connection-status feed and lists the connections broken
   right now. A connection that synced successfully *after* its bad status is skipped.
3. Asks an LLM to triage each one into a fixed set of next actions (`ask_user_to_reconnect`,
   `retry_later`, `provider_outage`, `needs_engineer`) through the half-price Batch API with
   a live fallback. An answer outside that set is escalated to `needs_engineer`, never guessed.
4. Writes the triage report atomically, plus a run-status file a monitor can alert on.

Real output of the offline run (fake LLM, simulated slow batch):

```
$ python examples/connection_doctor.py
INFO 4 broken connection(s): acct-0007, acct-0019, acct-0031, acct-0040
INFO batch batch_fake_1: 4 requests submitted
WARNING batch batch_fake_1 not done after 90s: cancelling; unfinished requests go live
INFO batch batch_fake_1 (cancelled): 2 from batch, 2 live, 0 failed
```

| connection | status | next action | answered by |
|---|---|---|---|
| acct-0007 | auth_expired since 2026-11-19 13:05:59Z (refresh token revoked) | ask_user_to_reconnect | batch |
| acct-0019 | provider_error since 2026-11-19 13:07:30Z (HTTP 503 from provider) | provider_outage | live |
| acct-0031 | mfa_required since 2026-11-19 13:10:44Z (one-time code requested) | ask_user_to_reconnect | batch |
| acct-0040 | unknown_error since 2026-11-19 13:11:20Z (unexpected page layout) | needs_engineer | live |

Two connections in the sample feed are left out on purpose: one synced successfully
after its rate-limit error, and one reported `ok` again.

`--live` switches to the real Anthropic SDK (`pip install anthropic`, `ANTHROPIC_API_KEY`).
Two real runs against the API (Claude Opus 5.5), one for each path:

- `--batch-wait 60`: the batch still reported "in progress" at 60 s, so the job cancelled
  it. All 4 answers had already finished and were kept: 4 from batch, 0 live, 0 failed.
- `--batch-wait 0`: cancelled right after submitting, nothing had finished, so all 4 went
  out as live calls with the server-side refusal fallback enabled: 0 from batch, 4 live,
  0 failed.

Both runs gave the same four actions. The model chose `retry_later` for the single 503
where the fake says `provider_outage`, which is fair: one 503 looks transient, and calling
an outage needs errors across many connections, which a per-connection prompt can't see.
No request was refused, so the fallback model itself never had to step in; refusal
handling is covered by the offline tests.

[`deploy/systemd/`](deploy/systemd/) has an example service + timer that runs the job
every 10 minutes, with a systemd timeout and memory cap as a second line of defense.

## The pieces

| Module | What it does | The failure it guards against |
|---|---|---|
| [`batch_fallback.py`](reliable_ops/batch_fallback.py) | Sends many LLM requests through the Batch API but never waits past the caller's deadline. Past it: cancel, keep the finished answers, send only the rest live with identical parameters. Live retries only for errors that can succeed on a second try. | Batches usually finish in minutes but sometimes take hours. A nightly job can't stall on that, and it can't drop requests either. |
| [`status_feed.py`](reliable_ops/status_feed.py) | Answers "is this broken right now?" from an append-only JSONL feed: reads only new bytes, skips half-written lines, survives truncation and log rotation, thread-safe, explicit fail-open / fail-closed. | One service records statuses all day; another must check the latest one per key before acting, without a database or re-reading the file. A missed "recovered" event must not leave something marked broken forever. |
| [`single_run.py`](reliable_ops/single_run.py) | Keeps scheduled jobs from hanging: timeout and no stdin on every command, kills the whole process tree, overall run deadline, stale-lock takeover, atomic writes, last-success status for a monitor. | A scheduled task set to "don't start while one is running" skips every later run once one gets stuck. The only symptom is output that stops changing. |
| [`deploy/safe_deploy.sh`](deploy/safe_deploy.sh) | Updates a live service safely: wait for it to stop, check live and staged file hashes, back up, install atomically, compile, run tests, restore and re-verify on any failure. | Unattended night deploys must never leave a bad file in place, and "the server changed since I planned this" must abort, not overwrite. |

## Where this came from

Each piece came out of a real problem in a private system I run on a Linux cloud
server (a news-driven market-data research and paper-trading pipeline built in Python).
That code stays private, so everything here is a **fresh, generic rewrite**: no
trading logic, no data, no credentials, no server details.

I built both the original and this rewrite with heavy help from an AI coding assistant
(Claude Code). I designed the behavior, wrote down the failure each piece guards against,
reviewed the code, and can walk through every line.

## Design notes

- **Inject time.** `sleep` and `clock` are parameters, so a test of "wait up to an hour,
  then cancel" runs in milliseconds and is deterministic.
- **Never lose a request silently.** Every batch request ends as `batch`, `live` or
  `failed`, with notes on why it left the batch path.
- **Retry only what can succeed.** Network errors, timeouts, 408/409/429 and 5xx are
  retried with backoff; a 400, 401/403 or 404 fails at once. The live client runs with the
  SDK's own retries off, so the two retry loops don't multiply.
- **Degrade, don't crash.** A down batch endpoint means everything goes live. An unreadable
  status feed is an explicit policy choice (`fail_open`), not an accident.
- **Verify, then act, then verify again.** The deploy script checks hashes before it
  touches anything and again after a restore, and its exit code says which outcome you got.
- **Test the failure modes,** not just the happy path: a half-written line, log rotation,
  a grandchild process holding pipes open after its parent is killed, a CRLF copy of a
  file, a syntax error in a staged file, a request that fails with a 400.

## Layout

```
reliable_ops/      batch_fallback.py, status_feed.py, single_run.py, fakes.py
deploy/            safe_deploy.sh, example.conf, systemd/ (service + timer)
examples/          connection_doctor.py, data/ (synthetic sample feed)
tests/             one test file per piece + a smoke test of the example
```

MIT licensed.

"""Read an append-only JSON Lines status feed incrementally and answer
"is this thing broken right now?" in microseconds.

Typical setup: one process (a websocket listener, a webhook receiver, a
poller) appends one JSON object per line as status events arrive::

    {"conn": "acct-0042", "status": "auth_expired", "t": "2026-11-19T14:03:07.120Z", "detail": "token revoked"}

Other processes need the *latest* status per key, on demand, without a
database and without re-reading a file that grows all day.

How it stays cheap and correct
------------------------------
* It remembers the byte offset it has read up to and reads only what was
  appended since (the first call reads the whole file once).
* It consumes complete lines only. A line the writer is still in the middle
  of writing is left for the next call, so a half-written record is never
  parsed as garbage or skipped forever.
* If the file shrinks (truncated) or is replaced by a new file (log
  rotation: different inode), it starts over from the beginning.
* Malformed lines are counted and skipped; one bad line never stops the reader.
* A lock makes refresh-and-read atomic, so threads can share one reader.

``HealthCheck`` adds the decision layer, including two safety rules:

* **Fail open or fail closed, on purpose.** If the feed can't be read, the
  caller chooses whether that means "assume fine" (don't block work because a
  side channel is down) or "assume broken".
* **Evidence beats a stale status.** Status feeds can miss a "recovered"
  event. If the caller has seen the thing work *after* the bad status (for
  example a successful sync), the bad status is treated as resolved.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Status:
    key: str
    code: str
    at: float                 # epoch seconds (UTC)
    detail: str | None = None


def parse_time(value) -> float:
    """Epoch seconds from an epoch number or an ISO-8601 string ("...Z" allowed,
    any number of fractional digits)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"unsupported time value {value!r}")
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    if "." in s:          # exactly 6 fractional digits: Python 3.10 accepts only 3 or 6
        head, _, rest = s.partition(".")
        digits = len(rest) - len(rest.lstrip("0123456789"))
        s = f"{head}.{rest[:digits][:6].ljust(6, '0')}{rest[digits:]}"
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


class StatusFeed:
    def __init__(self, path: str, *, key_field: str = "conn", code_field: str = "status",
                 time_field: str = "t", detail_field: str = "detail",
                 tracked_codes: Iterable[str] | None = None):
        self.path = path
        self.key_field, self.code_field = key_field, code_field
        self.time_field, self.detail_field = time_field, detail_field
        self.tracked = set(tracked_codes) if tracked_codes is not None else None
        self._offset = 0
        self._identity: tuple[int, int] | None = None
        self._latest: dict[str, Status] = {}
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.skipped_lines = 0
        self.restarts = 0                         # truncations / rotations seen

    # -- reading -------------------------------------------------------------
    def refresh(self) -> bool:
        """Read newly appended complete lines. False if the file can't be read."""
        with self._lock:
            return self._refresh_locked()

    def get(self, key: str) -> tuple[bool, Status | None]:
        """Refresh, then return (feed_readable, latest status for key)."""
        with self._lock:
            ok = self._refresh_locked()
            return ok, self._latest.get(key)

    def snapshot(self) -> tuple[bool, dict[str, Status]]:
        with self._lock:
            ok = self._refresh_locked()
            return ok, dict(self._latest)

    def _refresh_locked(self) -> bool:
        try:
            with open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                identity, size = (st.st_dev, st.st_ino), st.st_size
                if self._identity is not None and (identity != self._identity or size < self._offset):
                    self._offset, self.restarts = 0, self.restarts + 1
                    self._latest.clear()
                self._identity = identity
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.last_error = None
        end = chunk.rfind(b"\n")
        if end < 0:
            return True                           # nothing complete yet
        self._offset += end + 1
        for raw in chunk[:end].split(b"\n"):
            self._apply(raw)
        return True

    def _apply(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            rec = json.loads(raw)
            key, code = rec[self.key_field], rec[self.code_field]
            if not isinstance(key, str) or not isinstance(code, str):
                raise ValueError("key and code must be strings")
            at = parse_time(rec[self.time_field])
        except (ValueError, KeyError, TypeError):
            self.skipped_lines += 1
            return
        if self.tracked is not None and code not in self.tracked:
            return
        detail = rec.get(self.detail_field)
        self._latest[key] = Status(key, code, at, detail if isinstance(detail, str) else None)


class HealthCheck:
    def __init__(self, feed: StatusFeed, *, broken_codes: Iterable[str],
                 healthy_codes: Iterable[str] = (), fail_open: bool = True):
        self.feed = feed
        self.broken = set(broken_codes)
        self.healthy = set(healthy_codes)
        self.fail_open = fail_open

    def problem(self, key: str, last_success_at: float | None = None) -> str | None:
        """None if `key` should be treated as fine, else a human-readable reason.

        last_success_at: epoch time of the last thing that proved `key` works
        (e.g. a successful sync). A bad status older than that is ignored.
        """
        ok, status = self.feed.get(key)
        if not ok:
            return None if self.fail_open else f"status feed unreadable ({self.feed.last_error})"
        return self._judge(status, last_success_at)

    def all_problems(self) -> dict[str, str]:
        """Every key whose latest status is broken (no success evidence applied)."""
        ok, snap = self.feed.snapshot()
        if not ok:
            return {} if self.fail_open else {"*": f"status feed unreadable ({self.feed.last_error})"}
        out = {}
        for key, status in sorted(snap.items()):
            reason = self._judge(status, None)
            if reason:
                out[key] = reason
        return out

    def _judge(self, status: Status | None, last_success_at: float | None) -> str | None:
        if status is None or status.code in self.healthy or status.code not in self.broken:
            return None
        if last_success_at is not None and last_success_at > status.at:
            return None                           # it worked after the bad status: recovered
        since = dt.datetime.fromtimestamp(status.at, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        return f"{status.code} since {since}" + (f" ({status.detail})" if status.detail else "")

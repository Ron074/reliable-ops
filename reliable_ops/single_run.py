"""Helpers that keep a scheduled job from hanging forever, and from silently
stopping all future runs when it does.

The failure this guards against
-------------------------------
Schedulers commonly refuse to start a job while a previous run is still going
(Windows Task Scheduler's default, cron wrappers using a lock, systemd for the
same unit). That is the right default, but it has a nasty failure mode: if one
run hangs (a network copy that never times out, a child waiting for a password
prompt nobody will answer), **every later run is skipped**, quietly, until a
human notices the output has stopped changing.

The defenses, each small:

* ``run_cmd``: every external command has a timeout, gets no stdin (so it
  can't wait on a prompt), and on timeout its whole process tree is killed,
  not just the direct child.
* ``Deadline``: the run as a whole has a time budget; each step gets
  min(step timeout, time left).
* ``SingleInstanceLock``: one run at a time, but a lock whose heartbeat is
  older than the job could possibly take is treated as left by a dead or hung
  run and taken over, with a warning.
* ``atomic_write_text`` / ``write_run_status``: outputs are replaced
  atomically (no half-written files for readers), and every run records when
  it last succeeded so a separate monitor can alert on "no success in N
  minutes" instead of trusting the job to report its own death.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence

log = logging.getLogger(__name__)


# -- time budget --------------------------------------------------------------
class Deadline:
    def __init__(self, total_s: float, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._end = clock() + total_s

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0

    def budget(self, step_s: float) -> float:
        """Timeout for the next step: never more than what the whole run has left."""
        return min(step_s, self.remaining())


# -- external commands ----------------------------------------------------------
@dataclass
class CmdResult:
    args: Sequence[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_cmd(args: Sequence[str], timeout_s: float, *, deadline: Deadline | None = None,
            cwd: str | None = None, env: dict | None = None) -> CmdResult:
    """Run a command without a shell. Never raises for timeouts or a missing
    executable; the result says what happened."""
    limit = deadline.budget(timeout_s) if deadline else timeout_s
    start = time.monotonic()
    if limit <= 0:
        return CmdResult(args, None, "", "skipped: no time left in the run's deadline", True, 0.0)
    popen_kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", cwd=cwd, env=env)
    if os.name == "nt":
        popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True      # own process group, so the tree can be killed
    try:
        proc = subprocess.Popen(list(args), **popen_kw)
    except OSError as exc:
        return CmdResult(args, None, "", f"{type(exc).__name__}: {exc}", False,
                         time.monotonic() - start)
    try:
        out, err = proc.communicate(timeout=limit)
        return CmdResult(args, proc.returncode, out, err, False, time.monotonic() - start)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            # A grandchild that inherited our pipes can keep them open; don't wait on it forever.
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", "(output unavailable: pipes still held open after kill)"
        log.warning("command timed out after %.0fs and was killed: %s", limit, " ".join(args)[:200])
        return CmdResult(args, proc.returncode, out or "", err or "", True, time.monotonic() - start)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        proc.kill()


# -- one run at a time ------------------------------------------------------------
class LockHeld(RuntimeError):
    pass


class SingleInstanceLock:
    """File lock with stale-lock takeover.

    stale_after_s must be longer than the job can legitimately run between
    heartbeats (use the run's Deadline plus a margin). A lock file whose mtime is
    older than that belongs to a run that died or hung, and is taken over.
    """

    def __init__(self, path: str, stale_after_s: float, clock: Callable[[], float] = time.time):
        self.path = path
        self.stale_after_s = stale_after_s
        self._clock = clock
        self.held = False
        self.took_over: str | None = None         # description of the stale lock, if any

    def acquire(self) -> "SingleInstanceLock":
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                age = self._age()
                if age is None:
                    continue                      # vanished between our two calls; try again
                if age < self.stale_after_s:
                    raise LockHeld(f"{self.path} held by {self._owner()} (heartbeat {age:.0f}s ago)")
                self.took_over = f"{self._owner()}, heartbeat {age:.0f}s ago"
                log.warning("taking over stale lock %s (%s)", self.path, self.took_over)
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w") as f:
                json.dump({"pid": os.getpid(), "started": self._clock()}, f)
            self.held = True
            return self
        raise LockHeld(f"could not acquire {self.path}")

    def heartbeat(self) -> None:
        """Call between long steps so a slow-but-alive run isn't mistaken for a dead one."""
        if self.held:
            now = self._clock()
            os.utime(self.path, (now, now))

    def release(self) -> None:
        if self.held:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False

    def _age(self) -> float | None:
        try:
            return self._clock() - os.stat(self.path).st_mtime
        except FileNotFoundError:
            return None

    def _owner(self) -> str:
        try:
            with open(self.path, encoding="utf-8") as f:
                return f"pid {json.load(f).get('pid')}"
        except (OSError, ValueError, AttributeError):
            return "unknown owner"


# -- outputs -----------------------------------------------------------------------
def atomic_write_text(path: str, text: str) -> None:
    """Readers see the old file or the new file, never half of one."""
    tmp = f"{path}.part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_run_status(path: str, *, ok: bool, started: float, detail: str = "",
                     clock: Callable[[], float] = time.time) -> None:
    """Record this run's result; keeps the time of the last *successful* run."""
    previous = read_run_status(path) or {}
    now = clock()
    status = {
        "ok": ok,
        "started": started,
        "finished": now,
        "duration_s": round(now - started, 3),
        "detail": detail[:500],
        "last_success": now if ok else previous.get("last_success"),
    }
    atomic_write_text(path, json.dumps(status, indent=2) + "\n")


def read_run_status(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def overdue(path: str, max_age_s: float, clock: Callable[[], float] = time.time) -> str | None:
    """For a monitor: None if the job succeeded recently, else why it's overdue."""
    status = read_run_status(path)
    if not status or status.get("last_success") is None:
        return "no successful run recorded"
    age = clock() - status["last_success"]
    if age > max_age_s:
        return f"last success {age / 60:.0f} min ago (limit {max_age_s / 60:.0f} min)"
    return None

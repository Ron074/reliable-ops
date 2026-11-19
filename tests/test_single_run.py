import os
import sys
import tempfile
import time
import unittest

from reliable_ops.fakes import FakeClock
from reliable_ops.single_run import (Deadline, LockHeld, SingleInstanceLock, atomic_write_text,
                                     overdue, read_run_status, run_cmd, sha256_file,
                                     write_run_status)

PY = sys.executable


class DeadlineTests(unittest.TestCase):
    def test_budget_never_exceeds_time_left(self):
        clock = FakeClock()
        d = Deadline(100, clock=clock)
        self.assertEqual(d.budget(30), 30)
        clock.sleep(85)
        self.assertEqual(d.budget(30), 15)
        clock.sleep(20)
        self.assertTrue(d.expired())
        self.assertEqual(d.budget(30), 0)


class RunCmdTests(unittest.TestCase):
    def test_success(self):
        r = run_cmd([PY, "-c", "print('hi')"], timeout_s=30)
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout.strip(), "hi")

    def test_nonzero_exit(self):
        r = run_cmd([PY, "-c", "import sys; sys.exit(3)"], timeout_s=30)
        self.assertEqual((r.ok, r.returncode, r.timed_out), (False, 3, False))

    def test_hung_command_is_killed(self):
        t = time.monotonic()
        r = run_cmd([PY, "-c", "import time; time.sleep(60)"], timeout_s=1)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)
        self.assertLess(time.monotonic() - t, 20)

    def test_hung_grandchild_holding_pipes_is_killed_too(self):
        # The child starts a grandchild that inherits stdout and sleeps; killing only the
        # child would leave communicate() waiting on the grandchild's open pipe.
        code = ("import subprocess, sys, time;"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                "time.sleep(60)")
        t = time.monotonic()
        r = run_cmd([PY, "-c", code], timeout_s=1)
        self.assertTrue(r.timed_out)
        self.assertLess(time.monotonic() - t, 25)

    def test_no_stdin_so_prompts_cannot_block(self):
        r = run_cmd([PY, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout_s=30)
        self.assertEqual(r.stdout.strip(), "''")

    def test_missing_executable_is_a_result_not_an_exception(self):
        r = run_cmd(["definitely-not-a-real-program-xyz"], timeout_s=5)
        self.assertFalse(r.ok)
        self.assertIsNone(r.returncode)
        self.assertIn("Error", r.stderr)

    def test_spent_deadline_skips_the_command(self):
        clock = FakeClock()
        d = Deadline(10, clock=clock)
        clock.sleep(11)
        r = run_cmd([PY, "-c", "print('should not run')"], timeout_s=30, deadline=d)
        self.assertTrue(r.timed_out)
        self.assertIn("skipped", r.stderr)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "job.lock")

    def tearDown(self):
        self.dir.cleanup()

    def test_second_run_is_refused_while_first_is_alive(self):
        with SingleInstanceLock(self.path, stale_after_s=600):
            with self.assertRaises(LockHeld):
                SingleInstanceLock(self.path, stale_after_s=600).acquire()
        self.assertFalse(os.path.exists(self.path))      # released on exit

    def test_stale_lock_from_hung_run_is_taken_over(self):
        SingleInstanceLock(self.path, stale_after_s=600).acquire()   # never released: "hung"
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        lock = SingleInstanceLock(self.path, stale_after_s=600).acquire()
        self.assertTrue(lock.held)
        self.assertIn("heartbeat", lock.took_over)
        lock.release()

    def test_heartbeat_keeps_a_slow_run_alive(self):
        clock = FakeClock(start=time.time())
        lock = SingleInstanceLock(self.path, stale_after_s=600, clock=clock).acquire()
        clock.sleep(500)
        lock.heartbeat()
        clock.sleep(500)                                 # 1000 s since start, 500 since heartbeat
        with self.assertRaises(LockHeld):
            SingleInstanceLock(self.path, stale_after_s=600, clock=clock).acquire()
        lock.release()


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_atomic_write_leaves_no_temp_file(self):
        p = os.path.join(self.dir.name, "report.md")
        atomic_write_text(p, "one\n")
        atomic_write_text(p, "two\n")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "two\n")
        self.assertEqual(os.listdir(self.dir.name), ["report.md"])
        self.assertEqual(len(sha256_file(p)), 64)

    def test_status_keeps_last_success_through_failures(self):
        p = os.path.join(self.dir.name, "status.json")
        clock = FakeClock(start=10_000)
        self.assertEqual(overdue(p, 600, clock=clock), "no successful run recorded")
        write_run_status(p, ok=True, started=9_990, clock=clock)
        clock.sleep(300)
        write_run_status(p, ok=False, started=10_290, detail="upstream timeout", clock=clock)
        st = read_run_status(p)
        self.assertEqual((st["ok"], st["last_success"]), (False, 10_000))
        self.assertIsNone(overdue(p, 600, clock=clock))
        clock.sleep(400)
        self.assertIn("last success 12 min ago", overdue(p, 600, clock=clock))


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import threading
import unittest

from reliable_ops.status_feed import HealthCheck, StatusFeed, parse_time

BROKEN = {"auth_expired", "mfa_required", "provider_error"}
HEALTHY = {"ok"}


def line(conn, status, t, detail=None):
    rec = {"conn": conn, "status": status, "t": t}
    if detail:
        rec["detail"] = detail
    return json.dumps(rec) + "\n"


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "status.jsonl")
        self.feed = StatusFeed(self.path)
        self.health = HealthCheck(self.feed, broken_codes=BROKEN, healthy_codes=HEALTHY)

    def tearDown(self):
        self.dir.cleanup()

    def append(self, text, mode="a"):
        with open(self.path, mode, encoding="utf-8", newline="") as f:
            f.write(text)

    def test_latest_status_wins_and_reads_only_new_bytes(self):
        self.append(line("a", "ok", 100) + line("a", "auth_expired", 200, "token revoked"))
        self.assertEqual(self.health.problem("a"), "auth_expired since 1970-01-01 00:03:20Z (token revoked)")
        first = self.feed._offset
        self.append(line("a", "ok", 300))
        self.assertIsNone(self.health.problem("a"))
        self.assertGreater(self.feed._offset, first)
        self.assertEqual(self.feed._offset, os.path.getsize(self.path))

    def test_half_written_line_waits_for_its_newline(self):
        full = line("b", "provider_error", 50)
        self.append(full[:15])                          # writer is mid-line
        self.assertIsNone(self.health.problem("b"))
        self.assertEqual(self.feed._offset, 0)
        self.append(full[15:])
        self.assertIsNotNone(self.health.problem("b"))
        self.assertEqual(self.feed.skipped_lines, 0)

    def test_malformed_lines_are_skipped_not_fatal(self):
        self.append("not json\n" + json.dumps({"conn": "c"}) + "\n" + line("c", "mfa_required", 10))
        self.assertTrue(self.health.problem("c").startswith("mfa_required"))
        self.assertEqual(self.feed.skipped_lines, 2)

    def test_success_after_bad_status_counts_as_recovered(self):
        self.append(line("d", "auth_expired", 1_000))
        self.assertIsNotNone(self.health.problem("d", last_success_at=999))
        self.assertIsNone(self.health.problem("d", last_success_at=1_001))

    def test_unknown_codes_are_not_problems(self):
        self.append(line("e", "syncing", 5))
        self.assertIsNone(self.health.problem("e"))

    def test_missing_file_fail_open_vs_fail_closed(self):
        self.assertIsNone(self.health.problem("x"))
        closed = HealthCheck(self.feed, broken_codes=BROKEN, fail_open=False)
        self.assertTrue(closed.problem("x").startswith("status feed unreadable"))
        self.assertTrue(closed.all_problems()["*"].startswith("status feed unreadable"))

    def test_truncation_starts_over(self):
        self.append(line("f", "auth_expired", 1) * 5)
        self.assertIsNotNone(self.health.problem("f"))
        self.append(line("g", "ok", 2), mode="w")       # file truncated and rewritten, shorter
        self.assertIsNone(self.health.problem("f"))     # old state dropped
        self.assertEqual(self.feed.restarts, 1)

    def test_rotation_to_new_file_starts_over(self):
        self.append(line("h", "provider_error", 1))
        self.assertIsNotNone(self.health.problem("h"))
        new = self.path + ".new"
        with open(new, "w", encoding="utf-8", newline="") as f:  # same size or bigger, different file
            f.write(line("i", "provider_error", 2) + line("j", "ok", 3) * 3)
        os.replace(new, self.path)
        self.assertIsNone(self.health.problem("h"))
        self.assertIsNotNone(self.health.problem("i"))
        self.assertEqual(self.feed.restarts, 1)

    def test_tracked_codes_filter(self):
        feed = StatusFeed(self.path, tracked_codes={"auth_expired"})
        self.append(line("k", "auth_expired", 1) + line("k", "chatty_debug", 2))
        ok, status = feed.get("k")
        self.assertTrue(ok)
        self.assertEqual(status.code, "auth_expired")

    def test_all_problems_lists_broken_keys(self):
        self.append(line("a", "ok", 1) + line("b", "auth_expired", 2) + line("c", "provider_error", 3))
        self.assertEqual(sorted(self.health.all_problems()), ["b", "c"])

    def test_threads_share_one_reader(self):
        self.append(line("z", "ok", 0))
        errors = []

        def reader():
            try:
                for _ in range(200):
                    self.health.problem("z")
            except Exception as exc:                    # pragma: no cover - only on failure
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(6)]
        for t in threads:
            t.start()
        for i in range(200):
            self.append(line(f"k{i}", "auth_expired", i))
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.health.all_problems()), 200)
        self.assertEqual(self.feed.skipped_lines, 0)


class ParseTimeTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_time(12.5), 12.5)
        self.assertEqual(parse_time("1970-01-01T00:00:10Z"), 10.0)
        self.assertAlmostEqual(parse_time("1970-01-01T00:00:10.123456789Z"), 10.123456, places=6)
        self.assertAlmostEqual(parse_time("1970-01-01T00:00:10.5Z"), 10.5, places=6)      # 3.10-safe
        self.assertAlmostEqual(parse_time("1970-01-01T00:00:10.12+00:00"), 10.12, places=6)
        self.assertEqual(parse_time("1970-01-01T01:00:00+01:00"), 0.0)
        with self.assertRaises(ValueError):
            parse_time(True)


if __name__ == "__main__":
    unittest.main()

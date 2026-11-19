import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConnectionDoctorSmokeTest(unittest.TestCase):
    def test_offline_run_triages_only_currently_broken_connections(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "examples", "connection_doctor.py")],
                           capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| acct-0007 | auth_expired", r.stdout)
        self.assertIn("ask_user_to_reconnect | batch", r.stdout)
        self.assertIn("needs_engineer | live", r.stdout)
        self.assertNotIn("acct-0012", r.stdout)   # synced after its rate-limit status: recovered
        self.assertNotIn("acct-0024", r.stdout)   # back to ok
        self.assertFalse(os.path.exists(os.path.join(ROOT, "examples", "data", "connection_doctor.lock")))


if __name__ == "__main__":
    unittest.main()

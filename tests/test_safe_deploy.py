"""Runs deploy/safe_deploy.sh against throwaway directories. Needs bash with
coreutils (Linux, macOS, or Git Bash on Windows); skipped otherwise."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "safe_deploy.sh"


def find_bash():
    candidates = [os.environ.get("SAFE_DEPLOY_BASH")]
    if os.name == "nt":   # prefer Git Bash; System32\bash.exe is WSL and can't see C:/ paths
        candidates += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]
    else:
        candidates.append(shutil.which("bash"))
    return next((c for c in candidates if c and os.path.exists(c)), None)


BASH = find_bash()


def h(text: str) -> str:
    return hashlib.sha256(text.replace("\r", "").encode()).hexdigest()[:12]


def posix(p) -> str:
    return Path(p).as_posix()


OLD = "VERSION = 1\n"
NEW = "VERSION = 2\n"
CHECK = f'"{posix(sys.executable)}" -c "import app, sys; sys.exit(0 if app.VERSION == 2 else 1)"'


@unittest.skipUnless(BASH, "bash not found")
class SafeDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.app, self.stage = base / "app", base / "stage"
        self.app.mkdir()
        self.stage.mkdir()
        (self.app / "app.py").write_text(OLD, newline="\n")
        self.conf = base / "deploy.conf"

    def tearDown(self):
        self.tmp.cleanup()

    def deploy(self, staged=NEW, expect_old=None, expect_new=None, service_state="inactive",
               test_cmd=CHECK, files="app.py", extra=""):
        (self.stage / "app.py").write_text(staged, newline="\n")
        expect_old = expect_old or f"app.py={h(OLD)}"
        expect_new = expect_new or f"app.py={h(staged)}"
        self.conf.write_text("\n".join([
            f'APP_DIR="{posix(self.app)}"',
            f'STAGE_DIR="{posix(self.stage)}"',
            f'FILES="{files}"',
            f'EXPECT_OLD="{expect_old}"',
            f'EXPECT_NEW="{expect_new}"',
            'SERVICE="demo.service"',
            f'SERVICE_STATE_CMD="echo {service_state}"',
            "WAIT_MAX_S=0",
            f'PYTHON="{posix(sys.executable)}"',
            f"TEST_CMD='{test_cmd}'",
            extra,
        ]) + "\n", newline="\n")
        r = subprocess.run([BASH, posix(SCRIPT), posix(self.conf)], capture_output=True,
                           text=True, timeout=120)
        self.log = (self.app / "deploy.log").read_text() if (self.app / "deploy.log").exists() else ""
        return r.returncode

    def live(self, name="app.py"):
        return (self.app / name).read_text()

    def backups(self):
        root = self.app / ".deploy_backups"
        return list(root.iterdir()) if root.exists() else []

    def test_happy_path_installs_and_keeps_a_backup(self):
        self.assertEqual(self.deploy(), 0, self.log)
        self.assertEqual(self.live(), NEW)
        self.assertIn("DEPLOYED: app.py=" + h(NEW), self.log)
        (backup,) = self.backups()
        self.assertEqual((backup / "app.py").read_text(), OLD)

    def test_crlf_copy_hashes_the_same(self):
        (self.app / "app.py").write_text(OLD.replace("\n", "\r\n"), newline="")
        self.assertEqual(self.deploy(), 0, self.log)

    def test_live_file_changed_since_planning_aborts_untouched(self):
        (self.app / "app.py").write_text("VERSION = 1  # hotfix\n", newline="\n")
        self.assertEqual(self.deploy(), 1)
        self.assertIn("ABORTED", self.log)
        self.assertEqual(self.live(), "VERSION = 1  # hotfix\n")
        self.assertEqual(self.backups(), [])

    def test_wrong_staged_file_aborts_untouched(self):
        self.assertEqual(self.deploy(expect_new="app.py=000000000000"), 1)
        self.assertEqual(self.live(), OLD)

    def test_service_still_running_aborts_untouched(self):
        self.assertEqual(self.deploy(service_state="active"), 1)
        self.assertIn("still active", self.log)
        self.assertEqual(self.live(), OLD)

    def test_failing_tests_restore_the_old_file(self):
        self.assertEqual(self.deploy(staged="VERSION = 3\n"), 2, self.log)
        self.assertIn("tests failed", self.log)
        self.assertIn("RESTORED", self.log)
        self.assertEqual(self.live(), OLD)

    def test_syntax_error_restores_the_old_file(self):
        self.assertEqual(self.deploy(staged="VERSION = = 2\n"), 2, self.log)
        self.assertIn("byte-compile failed", self.log)
        self.assertEqual(self.live(), OLD)

    def test_new_file_is_removed_again_on_failure(self):
        (self.stage / "extra.py").write_text("X = 1\n", newline="\n")
        rc = self.deploy(staged="VERSION = 3\n", files="app.py extra.py",
                         expect_old=f"app.py={h(OLD)} extra.py=none",
                         expect_new=f"app.py={h('VERSION = 3' + chr(10))} extra.py={h('X = 1' + chr(10))}")
        self.assertEqual(rc, 2, self.log)
        self.assertFalse((self.app / "extra.py").exists())
        self.assertEqual(self.live(), OLD)

    def test_new_file_installed_on_success(self):
        (self.stage / "extra.py").write_text("X = 1\n", newline="\n")
        rc = self.deploy(files="app.py extra.py",
                         expect_old=f"app.py={h(OLD)} extra.py=none",
                         expect_new=f"app.py={h(NEW)} extra.py={h('X = 1' + chr(10))}")
        self.assertEqual(rc, 0, self.log)
        self.assertEqual(self.live("extra.py"), "X = 1\n")


if __name__ == "__main__":
    unittest.main()

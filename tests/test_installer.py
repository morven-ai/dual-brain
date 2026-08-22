#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "bin" / "dual-brain"


class InstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dual-brain-installer-")
        self.tmp = Path(self.temporary.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "plugin-data"
        self.transcripts = self.tmp / "transcripts"
        self.transcripts.mkdir()
        self.sentinel = self.home / "sentinel.txt"
        self.sentinel.write_text("unchanged\n", encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update(
            {
                "CLAUDE_PLUGIN_DATA": str(self.data),
                "DUAL_BRAIN_USER_HOME": str(self.home),
                "DUAL_BRAIN_TRANSCRIPT_DIR": str(self.transcripts),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(INSTALLER), *arguments],
            cwd=self.tmp,
            env=env or self.env,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )

    def plan(self, *, env: dict[str, str] | None = None) -> dict[str, object]:
        result = self.run_cli("plan", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_preflight_is_read_only_for_external_targets(self) -> None:
        before = self.sentinel.read_bytes()
        result = self.run_cli("preflight")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["supported"])
        self.assertEqual(self.sentinel.read_bytes(), before)
        self.assertFalse((self.home / ".local").exists())

    def test_unknown_shim_blocks_plan_without_overwrite(self) -> None:
        shim = self.home / ".local" / "bin" / "brain"
        shim.parent.mkdir(parents=True)
        shim.write_text("unknown\n", encoding="utf-8")
        result = self.run_cli("plan")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(shim.read_text(encoding="utf-8"), "unknown\n")

    def test_paseo_plan_fails_before_external_mutation(self) -> None:
        checkout = self.tmp / "paseo"
        checkout.mkdir()
        sentinel = checkout / "sentinel.txt"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        result = self.run_cli("plan", "--paseo", str(checkout))
        self.assertEqual(result.returncode, 2)
        self.assertIn("maintenance lease", result.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
        self.assertFalse((self.home / ".local").exists())
        self.assertFalse((self.data / "config.json").exists())
        self.assertFalse((self.data / "current").exists())

    def test_apply_verify_and_explicit_rollback(self) -> None:
        plan = self.plan()
        applied = self.run_cli("apply", "--plan", str(plan["planId"]))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        transaction = json.loads(applied.stdout)["transactionId"]
        current = self.data / "current"
        self.assertTrue(current.is_symlink())
        runtime = current.resolve(strict=True)
        self.assertEqual(runtime.parent, self.data / "runtimes")
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o555)
        config = json.loads((self.data / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["managedBy"], "dual-brain-v1")
        for name in ("dual-brain", "brain", "cc-slot"):
            shim = self.home / ".local" / "bin" / name
            self.assertTrue(os.access(shim, os.X_OK))
            self.assertIn("dual-brain-managed-v1", shim.read_text(encoding="utf-8"))
        verified = self.run_cli("verify")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["ok"])
        rolled_back = self.run_cli("rollback", transaction)
        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assertFalse(current.exists())
        self.assertFalse((self.data / "active-transaction.json").exists())
        self.assertFalse((self.data / "config.json").exists())
        for name in ("dual-brain", "brain", "cc-slot"):
            self.assertFalse((self.home / ".local" / "bin" / name).exists())
        self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "unchanged\n")

    def test_existing_config_change_requires_explicit_migration(self) -> None:
        plan = self.plan()
        applied = self.run_cli("apply", "--plan", str(plan["planId"]))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        changed = self.run_cli("plan", "--review-gate")
        self.assertEqual(changed.returncode, 2)
        self.assertIn("자동 수정하지 않습니다", changed.stderr)

    def test_old_transaction_cannot_rollback_newer_apply(self) -> None:
        first_plan = self.plan()
        first = self.run_cli("apply", "--plan", str(first_plan["planId"]))
        self.assertEqual(first.returncode, 0, first.stderr)
        first_id = json.loads(first.stdout)["transactionId"]
        second_plan = self.plan()
        second = self.run_cli("apply", "--plan", str(second_plan["planId"]))
        self.assertEqual(second.returncode, 0, second.stderr)
        second_id = json.loads(second.stdout)["transactionId"]
        stale = self.run_cli("rollback", first_id)
        self.assertEqual(stale.returncode, 2)
        self.assertIn("현재 active transaction이 아니므로", stale.stderr)
        active = json.loads((self.data / "active-transaction.json").read_text(encoding="utf-8"))
        self.assertEqual(active["transactionId"], second_id)
        current = self.data / "current"
        self.assertTrue(current.is_symlink())

    def test_active_transaction_rollback_rejects_target_drift(self) -> None:
        plan = self.plan()
        applied = self.run_cli("apply", "--plan", str(plan["planId"]))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        transaction_id = json.loads(applied.stdout)["transactionId"]
        shim = self.home / ".local" / "bin" / "brain"
        shim.write_text(shim.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
        rollback = self.run_cli("rollback", transaction_id)
        self.assertEqual(rollback.returncode, 2)
        self.assertIn("transaction 이후 target이 변경", rollback.stderr)
        self.assertIn("# drift", shim.read_text(encoding="utf-8"))

    def test_installer_lock_blocks_concurrent_apply(self) -> None:
        plan = self.plan()
        lock = self.data / "installer.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            applied = self.run_cli("apply", "--plan", str(plan["planId"]))
        finally:
            os.close(descriptor)
        self.assertEqual(applied.returncode, 2)
        self.assertIn("다른 dual-brain transaction", applied.stderr)
        self.assertFalse((self.data / "current").exists())

    def test_fingerprint_drift_blocks_apply(self) -> None:
        plan = self.plan()
        shim = self.home / ".local" / "bin" / "brain"
        shim.parent.mkdir(parents=True)
        shim.write_text("created after plan\n", encoding="utf-8")
        applied = self.run_cli("apply", "--plan", str(plan["planId"]))
        self.assertEqual(applied.returncode, 2)
        self.assertEqual(shim.read_text(encoding="utf-8"), "created after plan\n")
        self.assertFalse((self.data / "current").exists())

    def test_verify_failure_rolls_back_external_targets(self) -> None:
        fake_bin = self.tmp / "fake-bin"
        fake_bin.mkdir()
        fake_claude = fake_bin / "claude"
        fake_claude.write_text(
            "#!/usr/bin/env bash\nif [[ ${1:-} == --version ]]; then echo 'Claude fixture'; exit 0; fi\nexit 1\n",
            encoding="utf-8",
        )
        fake_claude.chmod(0o755)
        env = self.env.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        plan = self.plan(env=env)
        applied = self.run_cli("apply", "--plan", str(plan["planId"]), env=env)
        self.assertEqual(applied.returncode, 2)
        self.assertIn("verify 실패", applied.stderr)
        self.assertFalse((self.data / "current").exists())
        self.assertFalse((self.data / "config.json").exists())
        for name in ("dual-brain", "brain", "cc-slot"):
            self.assertFalse((self.home / ".local" / "bin" / name).exists())
        transactions = list((self.data / "transactions").iterdir())
        self.assertEqual(len(transactions), 1)
        self.assertTrue((transactions[0] / "rollback.json").is_file())


if __name__ == "__main__":
    unittest.main()

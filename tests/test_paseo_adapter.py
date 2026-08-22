#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "libexec" / "paseo-adapter"
ASSETS = ROOT / "assets" / "paseo"


class PaseoAdapterTest(unittest.TestCase):
    def test_provenance_is_pinned_without_redistributing_patch(self) -> None:
        provenance = json.loads((ASSETS / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(provenance["commitCount"], len(provenance["commits"]))
        self.assertEqual(provenance["integrationStatus"], "blocked-without-maintenance-lease")
        self.assertFalse(provenance["patchBundled"])
        self.assertNotIn("patchFile", provenance)
        self.assertEqual(len(provenance["patchSha256"]), 64)
        self.assertEqual([path.name for path in ASSETS.iterdir()], ["provenance.json"])

    def test_preflight_and_apply_fail_closed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dual-brain-paseo-") as directory:
            checkout = Path(directory)
            sentinel = checkout / "sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            before = sentinel.read_bytes()
            preflight = subprocess.run(
                [str(ADAPTER), "preflight", "--checkout", str(checkout)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(preflight.returncode, 1, preflight.stderr)
            payload = json.loads(preflight.stdout)
            self.assertFalse(payload["supported"])
            self.assertFalse(payload["maintenanceLeaseAvailable"])
            self.assertFalse(payload["mutationAllowed"])
            apply = subprocess.run(
                [str(ADAPTER), "apply", "--checkout", str(checkout)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(apply.returncode, 2)
            self.assertEqual(sentinel.read_bytes(), before)
            self.assertEqual(sorted(path.name for path in checkout.iterdir()), ["sentinel.txt"])


if __name__ == "__main__":
    unittest.main()

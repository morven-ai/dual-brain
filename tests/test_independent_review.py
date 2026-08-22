#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "libexec" / "independent-review"
GATE = ROOT / "hooks" / "independent-review-gate.sh"
ACTOR = ROOT / "hooks" / "lib" / "actor.js"


class IndependentReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dual-brain-review-")
        self.tmp = Path(self.temporary.name)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.config = self.data / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "proxyBaseUrl": "http://127.0.0.1:8317/v1",
                    "reviewGateEnabled": True,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.plan = self.tmp / "plan.md"
        self.plan.write_text(
            "# 배포 계획\n/srv/private/project와 https://example.invalid/path?token=fake를 검토한다.\n"
            "Bearer fake-review-secret 및 person@example.invalid를 노출하지 않는다.\n",
            encoding="utf-8",
        )
        self.log = self.tmp / "calls.jsonl"
        self.a_adapter = self.make_adapter("A")
        self.o_adapter = self.make_adapter("O")
        self.env = os.environ.copy()
        self.env.update(
            {
                "CLAUDE_PLUGIN_DATA": str(self.data),
                "DUAL_BRAIN_CONFIG": str(self.config),
                "INDEPENDENT_REVIEW_ACTOR_LIB": str(ACTOR),
                "INDEPENDENT_REVIEW_CLAUDE_ADAPTER": str(self.a_adapter),
                "INDEPENDENT_REVIEW_CODEX_ADAPTER": str(self.o_adapter),
                "FAKE_REVIEW_LOG": str(self.log),
            }
        )
        self.env.pop("ANTHROPIC_BASE_URL", None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def marker(self) -> Path:
        return self.data / "attestations" / "pending.json"

    def make_adapter(self, family: str) -> Path:
        adapter = self.tmp / f"adapter-{family}"
        adapter.write_text(
            f"""#!/usr/bin/env python3
import json, os, sys
request = json.load(sys.stdin)
mode = os.environ.get('FAKE_{family}_MODE', 'success')
with open(os.environ['FAKE_REVIEW_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps({{'family':'{family}','session_id':request['session_id'],'message':request['user_message']}}) + '\\n')
if mode != 'success':
    errors = {{'quota':'weekly limit reached', 'unavailable':'provider unavailable', 'timeout':'timeout', 'generic':'syntax error'}}
    code = 124 if mode == 'timeout' else 2
    print(json.dumps({{'model':'{family}','model_source':'runtime','stdout':'','stderr':errors[mode],
      'exit_code':code,'timed_out':mode == 'timeout','session_id':'','read_only':True,'resumed':False}}))
    raise SystemExit(code)
model = 'claude-sonnet-5' if '{family}' == 'A' else 'gpt-5.6-terra'
source = 'requested' if '{family}' == 'A' else 'runtime'
print(json.dumps({{'model':model,'model_source':source,
  'stdout':'검수 완료 /srv/private/result https://secret.invalid Bearer hidden-review-token',
  'stderr':'trace /srv/private/error','exit_code':0,'timed_out':False,
  'session_id':request['session_id'],'read_only':True,'resumed':False}}))
""",
            encoding="utf-8",
        )
        adapter.chmod(0o755)
        return adapter

    def run_review(self, actor: str, **environment: str) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env["DUAL_BRAIN_ACTOR_MODEL"] = actor
        env.update(environment)
        return subprocess.run(
            [str(REVIEW), "--plan-file", str(self.plan), "--cwd", str(self.tmp), "--timeout-sec", "5"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self) -> list[dict[str, str]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_cross_family_review_is_redacted_and_attested(self) -> None:
        result = self.run_review("claude-opus-5")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["reviewerFamily"], "O")
        self.assertEqual(output["reviewMode"], "cross-family")
        self.assertNotIn("/srv/private", output["review"])
        self.assertNotIn("secret.invalid", output["review"])
        self.assertNotIn("hidden-review-token", output["review"])
        attestation = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(attestation["actorFamily"], "A")
        self.assertEqual(attestation["reviewerFamily"], "O")
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        self.assertNotIn("/srv/private", self.calls()[0]["message"])

    def test_unavailable_primary_uses_fresh_same_pool(self) -> None:
        result = self.run_review("gpt-5.6-sol", FAKE_A_MODE="quota")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["reviewerFamily"], "O")
        self.assertEqual(output["reviewMode"], "fresh-context")
        self.assertEqual(output["primaryFailure"], "quota")
        calls = self.calls()
        self.assertEqual([call["family"] for call in calls], ["A", "O"])
        self.assertNotEqual(calls[0]["session_id"], calls[1]["session_id"])

    def test_generic_primary_failure_does_not_fallback_or_attest(self) -> None:
        result = self.run_review("claude-opus-5", FAKE_O_MODE="generic")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(self.marker.exists())

    def gate(self, plan: str, actor: str = "claude-opus-5") -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env["DUAL_BRAIN_ACTOR_MODEL"] = actor
        payload = {"tool_name": "ExitPlanMode", "tool_input": {"plan": plan}}
        return subprocess.run(
            [str(GATE)],
            env=env,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_gate_consumes_attestation_once(self) -> None:
        reviewed = self.run_review("claude-opus-5")
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        first = self.gate(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse(self.marker.exists())
        second = self.gate(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(second.returncode, 2)

    def test_gate_rejects_hash_mismatch(self) -> None:
        reviewed = self.run_review("claude-opus-5")
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        result = self.gate(self.plan.read_text(encoding="utf-8") + "변경\n")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.marker.exists())

    def test_corrupt_gate_config_fails_closed(self) -> None:
        self.config.write_text("{broken\n", encoding="utf-8")
        result = self.gate("# 배포 계획\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("config가 손상", result.stderr)

    def test_gate_disabled_is_noop(self) -> None:
        self.config.write_text(json.dumps({"reviewGateEnabled": False}) + "\n", encoding="utf-8")
        result = self.gate("# 배포 계획\n")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

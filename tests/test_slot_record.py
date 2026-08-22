#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SLOT = ROOT / "libexec" / "cc-slot"
HOOK = ROOT / "hooks" / "cc-slot-record.sh"


def write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


class SlotRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dual-brain-slot-test-")
        self.tmp = Path(self.temp.name)
        self.plugin_data = self.tmp / "plugin-data"
        self.plugin_data.mkdir()
        self.transcripts = self.tmp / "transcripts"
        self.transcripts.mkdir()
        self.tmux_state = self.tmp / "tmux-state.json"
        self.tmux_capture = self.tmp / "tmux-capture.jsonl"
        self.tmux_state.write_text("{}\n", encoding="utf-8")
        self.fake_tmux = self.tmp / "tmux"
        write_executable(
            self.fake_tmux,
            """#!/usr/bin/env python3
import json, os, pathlib, sys
state_path = pathlib.Path(os.environ["FAKE_TMUX_STATE"])
capture_path = pathlib.Path(os.environ["FAKE_TMUX_CAPTURE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
with capture_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:1] == ["has-session"]:
    target = args[args.index("-t") + 1].removeprefix("=")
    raise SystemExit(0 if state.get(target) else 1)
if args[:1] == ["display-message"]:
    print(os.environ.get("FAKE_TMUX_CURRENT", ""))
    raise SystemExit(0)
if args[:1] == ["new-session"]:
    slot = args[args.index("-s") + 1]
    state[slot] = True
    state_path.write_text(json.dumps(state), encoding="utf-8")
    raise SystemExit(0)
if args[:1] == ["attach-session"]:
    raise SystemExit(0)
raise SystemExit(64)
""",
        )
        self.config = self.plugin_data / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "tmuxBinary": str(self.fake_tmux),
                    "transcriptDir": str(self.transcripts),
                    "slots": {"cc": ["cc1", "cc2"], "cx": ["cx1", "cx2"]},
                    "ccArgs": ["--model", "claude-opus-5", "--settings", "{}"],
                    "cxArgs": ["--model", "gpt-5.6-sol", "--effort", "high"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.base_env = os.environ.copy()
        self.base_env.update(
            {
                "CLAUDE_PLUGIN_DATA": str(self.plugin_data),
                "FAKE_TMUX_STATE": str(self.tmux_state),
                "FAKE_TMUX_CAPTURE": str(self.tmux_capture),
            }
        )
        self.base_env.pop("DUAL_BRAIN_CONFIG", None)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def slot_map(self) -> Path:
        return self.plugin_data / "state" / "slot-map.json"

    def write_slot_map(self, mapping: dict[str, str]) -> None:
        self.slot_map.parent.mkdir(mode=0o700, exist_ok=True)
        self.slot_map.write_text(json.dumps(mapping) + "\n", encoding="utf-8")
        self.slot_map.chmod(0o600)

    def set_live(self, **slots: bool) -> None:
        self.tmux_state.write_text(json.dumps(slots) + "\n", encoding="utf-8")

    def run_slot(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SLOT), *args],
            env=self.base_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_hook(
        self,
        payload: dict[str, str],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        hook_env = self.base_env.copy()
        if env:
            hook_env.update(env)
        return subprocess.run(
            [str(HOOK)],
            input=json.dumps(payload),
            env=hook_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def captures(self) -> list[list[str]]:
        if not self.tmux_capture.exists():
            return []
        return [json.loads(line) for line in self.tmux_capture.read_text(encoding="utf-8").splitlines()]

    def test_live_slot_attaches_without_starting(self) -> None:
        self.set_live(cc1=True)
        result = self.run_slot("cc1")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.captures()
        self.assertEqual(commands[0], ["has-session", "-t", "=cc1"])
        self.assertEqual(commands[-1], ["attach-session", "-t", "=cc1"])
        self.assertFalse(any(command[:1] == ["new-session"] for command in commands))

    def test_fresh_refuses_to_replace_live_slot(self) -> None:
        self.set_live(cx1=True)
        result = self.run_slot("cx1", "--fresh")
        self.assertEqual(result.returncode, 75)
        self.assertIn("이미 실행 중", result.stderr)
        self.assertFalse(any(command[:1] == ["new-session"] for command in self.captures()))

    def test_dead_slot_resumes_recorded_sid(self) -> None:
        sid = str(uuid.uuid4())
        (self.transcripts / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        self.write_slot_map({"cc1": sid})
        result = self.run_slot("cc1")
        self.assertEqual(result.returncode, 0, result.stderr)
        new_command = next(command for command in self.captures() if command[:1] == ["new-session"])
        launched = shlex.split(new_command[-1])
        self.assertIn("--initial-backend", launched)
        self.assertEqual(launched[launched.index("--initial-backend") + 1], "native")
        self.assertEqual(launched[launched.index("--resume") + 1], sid)
        self.assertIn(f"DUAL_BRAIN_CONFIG={self.config}", launched)

    def test_cx_stale_sid_is_removed_and_started_fresh(self) -> None:
        sid = str(uuid.uuid4())
        self.write_slot_map({"cx1": sid})
        result = self.run_slot("cx1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stale", result.stdout)
        self.assertEqual(json.loads(self.slot_map.read_text(encoding="utf-8")), {})
        launched = shlex.split(next(c for c in self.captures() if c[:1] == ["new-session"])[-1])
        self.assertNotIn("--resume", launched)
        self.assertEqual(launched[launched.index("--initial-backend") + 1], "proxy")

    def test_duplicate_live_sid_blocks_resume(self) -> None:
        sid = str(uuid.uuid4())
        (self.transcripts / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        self.write_slot_map({"cc1": sid, "cx1": sid})
        self.set_live(cx1=True)
        result = self.run_slot("cc1")
        self.assertEqual(result.returncode, 76)
        self.assertIn("live slot [cx1]", result.stderr)
        self.assertFalse(any(command[:1] == ["new-session"] for command in self.captures()))

    def test_fresh_clears_only_current_mapping_with_mode_0600(self) -> None:
        sid = str(uuid.uuid4())
        other = str(uuid.uuid4())
        self.write_slot_map({"cc1": sid, "cx1": other})
        result = self.run_slot("cc1", "fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.slot_map.read_text(encoding="utf-8")), {"cx1": other})
        self.assertEqual(stat.S_IMODE(self.slot_map.stat().st_mode), 0o600)
        self.assertFalse(list(self.slot_map.parent.glob(".*.tmp")))

    def test_hook_without_config_is_noop(self) -> None:
        env = self.base_env.copy()
        env.pop("CLAUDE_PLUGIN_DATA", None)
        result = subprocess.run(
            [str(HOOK)],
            input=json.dumps({"session_id": str(uuid.uuid4()), "source": "startup"}),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_non_managed_slot_is_noop(self) -> None:
        result = self.run_hook(
            {"session_id": str(uuid.uuid4()), "source": "startup"},
            env={"CC_SLOT_OVERRIDE": "other"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.slot_map.exists())

    def test_hook_matching_expected_sid_records_and_acks_atomically(self) -> None:
        sid = str(uuid.uuid4())
        old_slot = str(uuid.uuid4())
        self.write_slot_map({"cc2": sid, "cx1": old_slot})
        runtime = self.tmp / "runtime" / "cc1"
        runtime.mkdir(parents=True)
        ack = runtime / "session-ack.json"
        result = self.run_hook(
            {"session_id": sid, "source": "resume"},
            env={
                "CC_SLOT_OVERRIDE": "cc1",
                "CLAUDE_BRAIN_EXPECTED_SID": sid,
                "CLAUDE_BRAIN_ACK_FILE": str(ack),
                "CLAUDE_BRAIN_STATE_DIR": str(runtime),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.slot_map.read_text(encoding="utf-8")), {"cc1": sid, "cx1": old_slot})
        self.assertEqual(json.loads(ack.read_text(encoding="utf-8"))["status"], "ok")
        self.assertEqual(stat.S_IMODE(self.slot_map.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(ack.stat().st_mode), 0o600)

    def test_hook_expected_sid_mismatch_fails_closed_without_map_change(self) -> None:
        expected = str(uuid.uuid4())
        observed = str(uuid.uuid4())
        self.write_slot_map({"cc1": expected})
        runtime = self.tmp / "runtime" / "cc1"
        runtime.mkdir(parents=True)
        ack = runtime / "session-ack.json"
        result = self.run_hook(
            {"session_id": observed, "source": "resume"},
            env={
                "CC_SLOT_OVERRIDE": "cc1",
                "CLAUDE_BRAIN_EXPECTED_SID": expected,
                "CLAUDE_BRAIN_ACK_FILE": str(ack),
                "CLAUDE_BRAIN_STATE_DIR": str(runtime),
            },
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("dual-brain SID 불일치", result.stderr)
        self.assertEqual(json.loads(self.slot_map.read_text(encoding="utf-8")), {"cc1": expected})
        self.assertEqual(json.loads(ack.read_text(encoding="utf-8"))["status"], "mismatch")

    def test_hook_startup_preserves_existing_different_sid(self) -> None:
        existing = str(uuid.uuid4())
        observed = str(uuid.uuid4())
        self.write_slot_map({"cc1": existing})
        result = self.run_hook(
            {"session_id": observed, "source": "startup"},
            env={"CC_SLOT_OVERRIDE": "cc1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.slot_map.read_text(encoding="utf-8")), {"cc1": existing})

    def test_created_files_contain_no_host_specific_runtime_dependencies(self) -> None:
        forbidden = ("/home" + "/ubuntu", "her" + "mes", "co" + "dex", "hap" + "py", "re" + "nice", "system" + "d")
        for path in (SLOT, HOOK):
            source = path.read_text(encoding="utf-8").lower()
            for value in forbidden:
                self.assertNotIn(value, source, f"{path}: {value}")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "libexec" / "claude-brain"
CONTROLLER = ROOT / "bin" / "brain"
LEAN_PROMPT = (ROOT / "assets" / "cx-lean.md").read_text(encoding="utf-8").strip()


def write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def pid_is_live(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return False
    return len(stat) > 2 and stat[2] != "Z"


class ClaudeBrainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="claude-brain-test-")
        self.tmp = Path(self.temp.name)
        self.native_capture = self.tmp / "native.json"
        self.proxy_capture = self.tmp / "proxy.json"
        fake_source = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ[\"CAPTURE_FILE\"], \"w\", encoding=\"utf-8\") as handle:
    json.dump({
        \"argv\": sys.argv[1:],
        \"argv0\": sys.argv[0],
        \"actor\": os.environ.get(\"DUAL_BRAIN_ACTOR_MODEL\"),
        \"baseUrl\": os.environ.get(\"ANTHROPIC_BASE_URL\"),
        \"authToken\": os.environ.get(\"ANTHROPIC_AUTH_TOKEN\"),
        \"contextWindow\": os.environ.get(\"CLAUDE_CODE_MAX_CONTEXT_TOKENS\"),
    }, handle)
"""
        self.native = self.tmp / "fake-native"
        self.proxy = self.tmp / "fake-proxy"
        write_executable(self.native, fake_source)
        write_executable(self.proxy, fake_source)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_launcher(self, args: list[str], capture: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "CLAUDE_BRAIN_NATIVE_BIN": str(self.native),
                "CLAUDE_BRAIN_PROXY_BIN": str(self.proxy),
                "CAPTURE_FILE": str(capture),
                "ANTHROPIC_BASE_URL": "http://stale-proxy.invalid",
                "ANTHROPIC_AUTH_TOKEN": "stale-token",
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "999",
                "DUAL_BRAIN_ACTOR_MODEL": "gpt-stale",
            }
        )
        return subprocess.run(
            [str(LAUNCHER), *args],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_lean_prompt(self, argv: list[str], *, present: bool) -> None:
        if not present:
            self.assertNotIn("--append-system-prompt", argv)
            self.assertNotIn(LEAN_PROMPT, argv)
            return
        self.assertEqual(argv.count("--append-system-prompt"), 1)
        prompt_index = argv.index("--append-system-prompt")
        self.assertEqual(argv[prompt_index + 1], LEAN_PROMPT)

    def test_native_dispatch_cleans_proxy_environment_and_preserves_args(self) -> None:
        sid = str(uuid.uuid4())
        result = self.run_launcher(
            [
                "--initial-backend",
                "native",
                "--",
                "--model",
                "claude-opus-5",
                "--resume",
                sid,
                "--settings",
                "{}",
            ],
            self.native_capture,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.native_capture.read_text(encoding="utf-8"))
        self.assertEqual(
            capture["argv"],
            ["--model", "claude-opus-5", "--resume", sid, "--settings", "{}"],
        )
        self.assertEqual(capture["actor"], "claude-opus-5")
        self.assert_lean_prompt(capture["argv"], present=False)
        self.assertIsNone(capture["baseUrl"])
        self.assertIsNone(capture["authToken"])
        self.assertIsNone(capture["contextWindow"])
        self.assertFalse(self.proxy_capture.exists())

    def test_sonnet_dispatch_uses_native_backend(self) -> None:
        result = self.run_launcher(
            ["--initial-backend", "proxy", "--", "--model", "claude-sonnet-5"],
            self.native_capture,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.native_capture.read_text(encoding="utf-8"))
        self.assertEqual(capture["actor"], "claude-sonnet-5")
        self.assert_lean_prompt(capture["argv"], present=False)
        self.assertFalse(self.proxy_capture.exists())

    def test_fable_dispatch_uses_native_backend_without_lean_prompt(self) -> None:
        result = self.run_launcher(
            ["--initial-backend", "proxy", "--", "--model", "claude-fable-5"],
            self.native_capture,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.native_capture.read_text(encoding="utf-8"))
        self.assertEqual(capture["actor"], "claude-fable-5")
        self.assert_lean_prompt(capture["argv"], present=False)
        self.assertFalse(self.proxy_capture.exists())

    def test_brain_controller_accepts_sonnet_target(self) -> None:
        env = os.environ.copy()
        env.pop("CLAUDE_BRAIN_STATE_DIR", None)
        result = subprocess.run(
            [str(CONTROLLER), "sonnet"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("managed cc/cx slot", result.stderr)
        self.assertNotIn("사용법", result.stderr)

    def test_proxy_dispatch_uses_existing_claudex_entrypoint(self) -> None:
        result = self.run_launcher(
            ["--initial-backend", "native", "--", "--model=gpt-5.6-sol", "--effort", "high"],
            self.proxy_capture,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.proxy_capture.read_text(encoding="utf-8"))
        self.assertEqual(
            capture["argv"],
            [
                "--append-system-prompt",
                LEAN_PROMPT,
                "--model=gpt-5.6-sol",
                "--effort",
                "high",
            ],
        )
        self.assert_lean_prompt(capture["argv"], present=True)
        self.assertEqual(capture["baseUrl"], "http://stale-proxy.invalid")
        self.assertFalse(self.native_capture.exists())

    def test_initial_proxy_backend_adds_lean_prompt_without_model_argument(self) -> None:
        result = self.run_launcher(
            ["--initial-backend", "proxy", "--", "--settings", "{}"],
            self.proxy_capture,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.proxy_capture.read_text(encoding="utf-8"))
        self.assertEqual(
            capture["argv"],
            ["--append-system-prompt", LEAN_PROMPT, "--settings", "{}"],
        )
        self.assert_lean_prompt(capture["argv"], present=True)
        self.assertFalse(self.native_capture.exists())

    def test_initial_backend_does_not_add_model_argument(self) -> None:
        result = self.run_launcher(
            ["--initial-backend", "native", "--", "--settings", "{}"],
            self.native_capture,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.native_capture.read_text(encoding="utf-8"))
        self.assertEqual(capture["argv"], ["--settings", "{}"])
        self.assertEqual(capture["actor"], "claude-native")

    def test_unknown_model_fails_before_exec(self) -> None:
        result = self.run_launcher(
            ["--initial-backend", "native", "--", "--model", "unknown-model"],
            self.native_capture,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("지원하지 않는 model ID", result.stderr)
        self.assertFalse(self.native_capture.exists())
        self.assertFalse(self.proxy_capture.exists())

    def test_config_resolves_native_binary(self) -> None:
        config = self.tmp / "config.json"
        config.write_text(json.dumps({"nativeBinary": str(self.native)}), encoding="utf-8")
        env = os.environ.copy()
        env.pop("CLAUDE_BRAIN_NATIVE_BIN", None)
        env.update(
            {
                "CLAUDE_BRAIN_CONFIG": str(config),
                "CAPTURE_FILE": str(self.native_capture),
            }
        )
        result = subprocess.run(
            [str(LAUNCHER), "--initial-backend", "native", "--", "--settings", "{}"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.native_capture.read_text(encoding="utf-8"))
        self.assertEqual(Path(capture["argv0"]), self.native.resolve())

    def test_path_binary_is_realpathed(self) -> None:
        path_bin = self.tmp / "path-bin"
        path_bin.mkdir()
        (path_bin / "claude").symlink_to(self.native)
        env = os.environ.copy()
        env.pop("CLAUDE_BRAIN_NATIVE_BIN", None)
        env.pop("CLAUDE_BRAIN_CONFIG", None)
        env.pop("CLAUDE_PLUGIN_DATA", None)
        env.update(
            {
                "PATH": os.pathsep.join((str(path_bin), str(Path(sys.executable).parent))),
                "CAPTURE_FILE": str(self.native_capture),
            }
        )
        result = subprocess.run(
            [str(LAUNCHER), "--initial-backend", "native", "--", "--settings", "{}"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.native_capture.read_text(encoding="utf-8"))
        self.assertEqual(Path(capture["argv0"]), self.native.resolve())

    def test_runtime_tree_has_no_live_host_path_assumptions(self) -> None:
        forbidden = ("/home" + "/ubuntu", "her" + "mes")
        paths = (LAUNCHER, CONTROLLER, ROOT / "assets" / "cx-lean.md")
        for path in paths:
            source = path.read_text(encoding="utf-8")
            for value in forbidden:
                self.assertNotIn(value, source, f"{path}: {value}")

    def test_supervisor_switches_backend_and_kills_descendant_group(self) -> None:
        capture = self.tmp / "supervisor-captures.jsonl"
        trigger = self.tmp / "trigger"
        grandchild_file = self.tmp / "grandchild.pid"
        fake = self.tmp / "fake-session-child"
        write_executable(
            fake,
            """#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
capture = pathlib.Path(os.environ[\"FAKE_CAPTURE\"])
with capture.open(\"a\", encoding=\"utf-8\") as handle:
    handle.write(json.dumps({
        \"argv\": sys.argv[1:],
        \"pid\": os.getpid(),
        \"pgid\": os.getpgrp(),
        \"actor\": os.environ.get(\"DUAL_BRAIN_ACTOR_MODEL\"),
    }) + \"\\n\")
if any(arg == \"gpt-5.6-sol\" or arg == \"--model=gpt-5.6-sol\" for arg in sys.argv[1:]):
    time.sleep(0.3)
    raise SystemExit(0)
grandchild = subprocess.Popen([
    sys.executable,
    \"-c\",
    \"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)\",
])
pathlib.Path(os.environ[\"FAKE_GRANDCHILD\"]).write_text(str(grandchild.pid), encoding=\"utf-8\")
trigger = pathlib.Path(os.environ[\"FAKE_TRIGGER\"])
while not trigger.exists():
    time.sleep(0.05)
subprocess.run([os.environ[\"BRAIN_BIN\"], trigger.read_text(encoding=\"utf-8\").strip()], check=False)
raise SystemExit(88)
""",
        )
        sid = str(uuid.uuid4())
        transcript_dir = self.tmp / "transcripts"
        transcript_dir.mkdir()
        (transcript_dir / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        slot_map = self.tmp / "slot-map.json"
        slot_map.write_text(json.dumps({"cc1": sid}), encoding="utf-8")
        runtime_root = self.tmp / "runtime"
        env = os.environ.copy()
        env.update(
            {
                "CLAUDE_BRAIN_NATIVE_BIN": str(fake),
                "CLAUDE_BRAIN_PROXY_BIN": str(fake),
                "CLAUDE_BRAIN_RUNTIME_ROOT": str(runtime_root),
                "CLAUDE_BRAIN_SLOT_MAP": str(slot_map),
                "CLAUDE_BRAIN_SKIP_TMUX_DUPLICATE_CHECK": "1",
                "CLAUDE_BRAIN_SKIP_ACK": "1",
                "FAKE_CAPTURE": str(capture),
                "FAKE_TRIGGER": str(trigger),
                "FAKE_GRANDCHILD": str(grandchild_file),
                "BRAIN_BIN": str(CONTROLLER),
            }
        )
        supervisor = subprocess.Popen(
            [
                str(LAUNCHER),
                "supervise",
                "--initial-backend",
                "native",
                "--slot",
                "cc1",
                "--transcript-dir",
                str(transcript_dir),
                "--slot-map",
                str(slot_map),
                "--",
                "--settings",
                "{}",
            ],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            status_path = runtime_root / "cc1" / "status.json"
            self.assertTrue(
                wait_until(
                    lambda: status_path.exists()
                    and json.loads(status_path.read_text(encoding="utf-8")).get("phase") == "running"
                    and grandchild_file.exists(),
                    timeout=5,
                ),
                "initial supervisor state missing",
            )
            grandchild_pid = int(grandchild_file.read_text(encoding="utf-8"))
            trigger.write_text("sol", encoding="utf-8")
            stdout, stderr = supervisor.communicate(timeout=10)
            self.assertEqual(supervisor.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
            records = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 2, records)
            self.assertEqual(records[0]["actor"], "claude-native")
            self.assert_lean_prompt(records[0]["argv"], present=False)
            self.assertEqual(records[0]["pid"], records[0]["pgid"])
            self.assert_lean_prompt(records[1]["argv"], present=True)
            self.assertIn("--resume", records[1]["argv"])
            self.assertIn(sid, records[1]["argv"])
            self.assertIn("gpt-5.6-sol", records[1]["argv"])
            self.assertNotEqual(records[0]["pid"], records[1]["pid"])
            self.assertTrue(wait_until(lambda: not pid_is_live(grandchild_pid), timeout=3))
        finally:
            if supervisor.poll() is None:
                os.kill(supervisor.pid, signal.SIGTERM)
                supervisor.wait(timeout=5)

    def test_supervisor_removes_lean_prompt_when_switching_to_native(self) -> None:
        capture = self.tmp / "reverse-captures.jsonl"
        trigger = self.tmp / "reverse-trigger"
        grandchild_file = self.tmp / "reverse-grandchild.pid"
        fake = self.tmp / "fake-reverse-child"
        write_executable(
            fake,
            """#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
capture = pathlib.Path(os.environ["FAKE_CAPTURE"])
prior_count = len(capture.read_text(encoding="utf-8").splitlines()) if capture.exists() else 0
with capture.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "argv": sys.argv[1:],
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "actor": os.environ.get("DUAL_BRAIN_ACTOR_MODEL"),
    }) + "\\n")
if prior_count:
    time.sleep(0.3)
    raise SystemExit(0)
grandchild = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
pathlib.Path(os.environ["FAKE_GRANDCHILD"]).write_text(str(grandchild.pid), encoding="utf-8")
trigger = pathlib.Path(os.environ["FAKE_TRIGGER"])
while not trigger.exists():
    time.sleep(0.05)
subprocess.run([os.environ["BRAIN_BIN"], trigger.read_text(encoding="utf-8").strip()], check=False)
raise SystemExit(88)
""",
        )
        sid = str(uuid.uuid4())
        transcript_dir = self.tmp / "reverse-transcripts"
        transcript_dir.mkdir()
        (transcript_dir / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        slot_map = self.tmp / "reverse-slot-map.json"
        slot_map.write_text(json.dumps({"cx1": sid}), encoding="utf-8")
        runtime_root = self.tmp / "reverse-runtime"
        env = os.environ.copy()
        env.update(
            {
                "CLAUDE_BRAIN_NATIVE_BIN": str(fake),
                "CLAUDE_BRAIN_PROXY_BIN": str(fake),
                "CLAUDE_BRAIN_RUNTIME_ROOT": str(runtime_root),
                "CLAUDE_BRAIN_SLOT_MAP": str(slot_map),
                "CLAUDE_BRAIN_SKIP_TMUX_DUPLICATE_CHECK": "1",
                "CLAUDE_BRAIN_SKIP_ACK": "1",
                "FAKE_CAPTURE": str(capture),
                "FAKE_TRIGGER": str(trigger),
                "FAKE_GRANDCHILD": str(grandchild_file),
                "BRAIN_BIN": str(CONTROLLER),
            }
        )
        supervisor = subprocess.Popen(
            [
                str(LAUNCHER),
                "supervise",
                "--initial-backend",
                "proxy",
                "--slot",
                "cx1",
                "--transcript-dir",
                str(transcript_dir),
                "--slot-map",
                str(slot_map),
                "--",
                "--model",
                "gpt-5.6-sol",
                "--settings",
                "{}",
            ],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            status_path = runtime_root / "cx1" / "status.json"
            self.assertTrue(
                wait_until(
                    lambda: status_path.exists()
                    and json.loads(status_path.read_text(encoding="utf-8")).get("phase")
                    == "running"
                    and grandchild_file.exists(),
                    timeout=5,
                ),
                "initial reverse supervisor state missing",
            )
            grandchild_pid = int(grandchild_file.read_text(encoding="utf-8"))
            trigger.write_text("opus", encoding="utf-8")
            stdout, stderr = supervisor.communicate(timeout=10)
            self.assertEqual(supervisor.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
            records = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 2, records)
            self.assert_lean_prompt(records[0]["argv"], present=True)
            self.assertEqual(records[0]["pid"], records[0]["pgid"])
            self.assert_lean_prompt(records[1]["argv"], present=False)
            self.assertEqual(records[1]["actor"], "claude-opus-5")
            self.assertIn("--resume", records[1]["argv"])
            self.assertIn(sid, records[1]["argv"])
            self.assertIn("claude-opus-5", records[1]["argv"])
            self.assertNotEqual(records[0]["pid"], records[1]["pid"])
            self.assertTrue(wait_until(lambda: not pid_is_live(grandchild_pid), timeout=3))
        finally:
            if supervisor.poll() is None:
                os.kill(supervisor.pid, signal.SIGTERM)
                supervisor.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Fresh native Claude read-only reviewer adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def config() -> dict[str, Any]:
    value = os.environ.get("DUAL_BRAIN_CONFIG")
    if not value and os.environ.get("CLAUDE_PLUGIN_DATA"):
        value = str(Path(os.environ["CLAUDE_PLUGIN_DATA"]) / "config.json")
    if not value:
        return {}
    try:
        loaded = json.loads(Path(value).read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def executable() -> str:
    candidate = config().get("nativeBinary") or shutil.which("claude")
    if not isinstance(candidate, str) or not candidate:
        raise RuntimeError("claude 실행 파일이 없습니다")
    resolved = Path(candidate).expanduser().resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError("claude 실행 파일이 아닙니다")
    return str(resolved)


def emit(request_id: str, **values: Any) -> None:
    payload = {
        "request_id": request_id,
        "model": values.get("model", ""),
        "model_source": "requested",
        "stdout": values.get("stdout", ""),
        "stderr": values.get("stderr", ""),
        "exit_code": values.get("exit_code", 1),
        "duration_ms": values.get("duration_ms", 0),
        "timed_out": values.get("timed_out", False),
        "truncated": values.get("truncated", False),
        "session_id": values.get("session_id", ""),
        "read_only": True,
        "resumed": False,
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main() -> int:
    started = time.monotonic()
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        emit("invalid", stderr="입력 JSON이 올바르지 않습니다")
        return 1
    request_id = request.get("request_id", "invalid")
    try:
        session_id = request["session_id"]
        system_prompt = request["system_prompt"]
        user_message = request["user_message"]
        cwd = str(Path(request["cwd"]).resolve(strict=True))
        timeout = int(request.get("timeout_sec", 180))
        max_chars = int(request.get("max_chars", 12000))
        model = os.environ.get("CLAUDE_REVIEW_MODEL", "opus")
        command = [
            executable(),
            "--safe-mode",
            "-p",
            "--output-format",
            "json",
            "--session-id",
            session_id,
            "--model",
            model,
            "--tools",
            "",
            "--append-system-prompt",
            system_prompt,
            user_message,
        ]
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        output = result.stdout
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict) and isinstance(parsed.get("result"), str):
                output = parsed["result"]
        except json.JSONDecodeError:
            pass
        truncated = len(output) > max_chars
        emit(
            request_id,
            model=model,
            stdout=output[:max_chars],
            stderr=result.stderr[:max_chars],
            exit_code=result.returncode,
            duration_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated,
            session_id=session_id,
        )
        return result.returncode
    except subprocess.TimeoutExpired as error:
        emit(
            request_id,
            model=os.environ.get("CLAUDE_REVIEW_MODEL", "opus"),
            stderr=f"timeout: {error}",
            exit_code=124,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=True,
            session_id=request.get("session_id", ""),
        )
        return 124
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
        emit(request_id, stderr=str(error), duration_ms=int((time.monotonic() - started) * 1000))
        return 1


raise SystemExit(main())

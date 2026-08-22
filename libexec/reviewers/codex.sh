#!/usr/bin/env python3
"""Fresh Codex read-only reviewer adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.I)


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
    candidate = config().get("codexBinary") or shutil.which("codex-cc")
    if not isinstance(candidate, str) or not candidate:
        raise RuntimeError("codex-cc 실행 파일이 없습니다")
    resolved = Path(candidate).expanduser().resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError("codex-cc 실행 파일이 아닙니다")
    return str(resolved)


def values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if current_key == key:
                found.append(current_value)
            found.extend(values(current_value, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(values(item, key))
    return found


def first_string(payload: Any, keys: tuple[str, ...]) -> str:
    for key in keys:
        for value in values(payload, key):
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def emit(request_id: str, **items: Any) -> None:
    payload = {
        "request_id": request_id,
        "model": items.get("model", ""),
        "model_source": "runtime" if items.get("model") else "requested",
        "stdout": items.get("stdout", ""),
        "stderr": items.get("stderr", ""),
        "exit_code": items.get("exit_code", 1),
        "duration_ms": items.get("duration_ms", 0),
        "timed_out": items.get("timed_out", False),
        "truncated": items.get("truncated", False),
        "session_id": items.get("session_id", ""),
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
        cwd = str(Path(request["cwd"]).resolve(strict=True))
        timeout = int(request.get("timeout_sec", 180))
        max_chars = int(request.get("max_chars", 12000))
        prompt = f"{request['system_prompt']}\n\n---\n\n{request['user_message']}"
        command = [executable(), "task", "--read-only", "--json", "-C", cwd]
        tier = os.environ.get("CODEX_REVIEW_TIER", "")
        if tier:
            command.extend(["--tier", tier])
        command.append(prompt)
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        parsed: Any = None
        for line in reversed(result.stdout.splitlines()):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        model = first_string(parsed, ("resolved_model", "resolvedModel", "model"))
        session_id = first_string(parsed, ("sessionId", "session_id"))
        combined = f"{result.stdout}\n{result.stderr}"
        if not session_id:
            match = re.search(r"session id:\s*([0-9a-f-]{36})", combined, re.I)
            session_id = match.group(1) if match else ""
        if session_id and not UUID_RE.fullmatch(session_id):
            session_id = ""
        output = first_string(parsed, ("result", "output", "final_output", "text")) or result.stdout
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
            stderr=f"timeout: {error}",
            exit_code=124,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=True,
        )
        return 124
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
        emit(request_id, stderr=str(error), duration_ms=int((time.monotonic() - started) * 1000))
        return 1


raise SystemExit(main())

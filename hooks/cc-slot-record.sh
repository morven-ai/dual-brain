#!/usr/bin/env bash
set -euo pipefail

read -r -d '' PYTHON_CODE <<'PY' || true
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
EXPECTED = os.environ.get("CLAUDE_BRAIN_EXPECTED_SID", "")
ACK_VALUE = os.environ.get("CLAUDE_BRAIN_ACK_FILE", "")
STATE_VALUE = os.environ.get("CLAUDE_BRAIN_STATE_DIR", "")


class HookError(RuntimeError):
    pass


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise HookError(f"안전하지 않은 state directory입니다: {parent}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def ack(status: str, sid: str = "", message: str = "") -> None:
    if not EXPECTED:
        return
    if not ACK_VALUE or not STATE_VALUE:
        raise HookError("dual-brain SID ack/state 경로가 없습니다")
    state_dir = Path(STATE_VALUE).expanduser()
    ack_path = Path(ACK_VALUE).expanduser()
    if ack_path.is_symlink():
        raise HookError("dual-brain SID ack 파일이 symlink입니다")
    try:
        state_resolved = state_dir.resolve(strict=True)
        parent_resolved = ack_path.parent.resolve(strict=True)
    except OSError as error:
        raise HookError(f"dual-brain SID ack 경로를 해석할 수 없습니다: {error}") from error
    if state_dir.is_symlink() or parent_resolved != state_resolved:
        raise HookError("dual-brain SID ack 경로가 state 밖입니다")
    payload: dict[str, Any] = {"status": status, "sid": sid, "expectedSid": EXPECTED}
    if message:
        payload["message"] = message
    atomic_json_write(ack_path, payload)


def fail_closed(message: str, sid: str = "") -> int:
    try:
        ack("error", sid, message)
    except HookError as ack_error:
        print(f"cc-slot-record: {ack_error}", file=sys.stderr)
    print(f"cc-slot-record: {message}", file=sys.stderr)
    return 2


def config_path() -> Path | None:
    explicit = os.environ.get("DUAL_BRAIN_CONFIG")
    if explicit:
        path = Path(explicit).expanduser()
    else:
        plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
        if not plugin_data:
            return None
        path = Path(plugin_data).expanduser() / "config.json"
    return path if path.is_file() else None


def load_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise HookError(f"config를 읽을 수 없습니다: {path}: {error}") from error
    if not isinstance(value, dict):
        raise HookError(f"config object가 아닙니다: {path}")
    return value


def string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise HookError(f"config {field}는 비어 있지 않은 문자열 배열이어야 합니다")
    return list(value)


def managed_slots(config: dict[str, Any]) -> set[str]:
    cc_slots = string_list(config.get("ccSlots"), "ccSlots")
    cx_slots = string_list(config.get("cxSlots"), "cxSlots")
    slots = config.get("slots")
    if slots is not None:
        if not isinstance(slots, dict):
            raise HookError("config slots는 object여야 합니다")
        if "cc" in slots or "cx" in slots:
            cc_slots = string_list(slots.get("cc"), "slots.cc")
            cx_slots = string_list(slots.get("cx"), "slots.cx")
        else:
            cc_slots = [name for name, kind in slots.items() if kind in {"cc", "native"}]
            cx_slots = [name for name, kind in slots.items() if kind in {"cx", "proxy"}]
            unknown = [name for name, kind in slots.items() if kind not in {"cc", "cx", "native", "proxy"}]
            if unknown:
                raise HookError(f"config slot 종류가 올바르지 않습니다: {unknown[0]}")
    result = set(cc_slots + cx_slots)
    if len(result) != len(cc_slots) + len(cx_slots):
        raise HookError("config slot이 중복 정의됐습니다")
    for slot in result:
        if not SLOT_RE.fullmatch(slot):
            raise HookError(f"안전하지 않은 slot 이름입니다: {slot}")
    return result


def state_root(path: Path) -> Path:
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    return (Path(plugin_data).expanduser() if plugin_data else path.parent) / "state"


def ensure_state_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise HookError(f"안전하지 않은 state directory입니다: {path}")
    os.chmod(path, 0o700)


def read_mapping(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise HookError(f"slot-map이 symlink입니다: {path}")
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise HookError(f"slot-map을 읽을 수 없습니다: {path}: {error}") from error
    if not isinstance(value, dict):
        raise HookError("slot-map object가 아닙니다")
    result: dict[str, str] = {}
    for slot, sid in value.items():
        if not isinstance(slot, str) or not isinstance(sid, str):
            raise HookError("slot-map entry가 문자열이 아닙니다")
        try:
            parsed = uuid.UUID(sid)
        except ValueError as error:
            raise HookError(f"slot-map SID 형식이 올바르지 않습니다: {slot}={sid}") from error
        if str(parsed) != sid:
            raise HookError(f"slot-map SID가 canonical UUID가 아닙니다: {slot}={sid}")
        result[slot] = sid
    os.chmod(path, 0o600)
    return result


def main() -> int:
    path = config_path()
    if path is None:
        return fail_closed("dual-brain config가 없습니다") if EXPECTED else 0
    try:
        config = load_config(path)
        slots = managed_slots(config)
    except HookError as error:
        return fail_closed(str(error)) if EXPECTED else 2

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        event = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return fail_closed("SessionStart JSON이 올바르지 않습니다") if EXPECTED else 0
    if not isinstance(event, dict):
        return fail_closed("SessionStart payload가 object가 아닙니다") if EXPECTED else 0
    sid = event.get("session_id", "")
    if not isinstance(sid, str) or not sid:
        return fail_closed("SessionStart session_id가 없습니다") if EXPECTED else 0
    try:
        parsed_sid = uuid.UUID(sid)
    except ValueError:
        return fail_closed(f"SessionStart SID 형식이 올바르지 않습니다: {sid}", sid) if EXPECTED else 0
    if str(parsed_sid) != sid:
        return fail_closed(f"SessionStart SID가 canonical UUID가 아닙니다: {sid}", sid) if EXPECTED else 0

    slot = os.environ.get("CC_SLOT_OVERRIDE", "")
    if not slot and os.environ.get("TMUX"):
        tmux_value = config.get("tmuxBinary", "tmux")
        if not isinstance(tmux_value, str) or not tmux_value:
            return fail_closed("config tmuxBinary가 올바르지 않습니다", sid) if EXPECTED else 2
        result = subprocess.run(
            [tmux_value, "display-message", "-p", "#S"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            slot = result.stdout.strip()
    if not slot:
        return fail_closed("managed slot을 확인할 수 없습니다", sid) if EXPECTED else 0
    if slot not in slots:
        return fail_closed(f"managed cc/cx slot이 아닙니다: {slot}", sid) if EXPECTED else 0

    if EXPECTED:
        try:
            parsed_expected = uuid.UUID(EXPECTED)
        except ValueError:
            return fail_closed(f"expected SID 형식이 올바르지 않습니다: {EXPECTED}", sid)
        if str(parsed_expected) != EXPECTED:
            return fail_closed(f"expected SID가 canonical UUID가 아닙니다: {EXPECTED}", sid)
        if sid != EXPECTED:
            try:
                ack("mismatch", sid)
            except HookError as error:
                print(f"cc-slot-record: {error}", file=sys.stderr)
            print(f"dual-brain SID 불일치: observed={sid} expected={EXPECTED}", file=sys.stderr)
            return 2

    root = state_root(path)
    try:
        ensure_state_dir(root)
        slot_map = root / "slot-map.json"
        lock_path = root / "slot-map.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            mapping = read_mapping(slot_map)
            existing = mapping.get(slot)
            source = event.get("source", "")
            if EXPECTED or not (source == "startup" and existing and existing != sid):
                mapping = {name: value for name, value in mapping.items() if name == slot or value != sid}
                mapping[slot] = sid
                atomic_json_write(slot_map, mapping)
        finally:
            os.close(lock_fd)
        if EXPECTED:
            ack("ok", sid)
    except HookError as error:
        return fail_closed(str(error), sid) if EXPECTED else 2
    return 0


raise SystemExit(main())
PY

exec python3 -c "$PYTHON_CODE"

#!/usr/bin/env bash
set -uo pipefail

INPUT="$(cat 2>/dev/null)"
[[ -n "$INPUT" ]] || exit 0
[[ "$(jq -r '.tool_name // ""' <<<"$INPUT" 2>/dev/null)" == "ExitPlanMode" ]] || exit 0

PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-}"
[[ -n "$PLUGIN_DATA" ]] || exit 0
CONFIG="${DUAL_BRAIN_CONFIG:-$PLUGIN_DATA/config.json}"
[[ -f "$CONFIG" ]] || exit 0
if ! jq -e 'type == "object" and ((has("reviewGateEnabled") | not) or (.reviewGateEnabled | type == "boolean"))' "$CONFIG" >/dev/null 2>&1; then
  echo "independent-review gate: config가 손상됐거나 reviewGateEnabled 타입이 올바르지 않습니다" >&2
  exit 2
fi
[[ "$(jq -r '.reviewGateEnabled // false' "$CONFIG")" == "true" ]] || exit 0

PLAN="$(jq -r '(.tool_input.plan // "") | if type == "string" then . else "" end' <<<"$INPUT" 2>/dev/null)"
[[ -n "$PLAN" ]] || exit 0
KW_RE='비가역|irreversible|마이그레이션|migration|배포|deploy|cutover|rollback +impossible|롤백 +불가|파괴적|destructive|결제|payment|DROP +TABLE|TRUNCATE[[:space:]]+TABLE'
MATCHED="$(grep -oiE "$KW_RE" <<<"$PLAN" | head -1 || true)"
[[ -n "$MATCHED" ]] || exit 0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER="$PLUGIN_DATA/attestations/pending.json"
ACTOR_MODEL="$({ printf '%s' "$INPUT" | ACTOR_LIB="$ROOT/hooks/lib/actor.js" node -e '
let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => { raw += chunk; });
process.stdin.on("end", () => {
  try {
    const payload = JSON.parse(raw);
    const { resolveActorModel } = require(process.env.ACTOR_LIB);
    process.stdout.write(resolveActorModel(payload));
  } catch {}
});
'; } 2>/dev/null)"
case "$ACTOR_MODEL" in
  gpt-*) ACTOR_FAMILY="O" ;;
  claude-*) ACTOR_FAMILY="A" ;;
  *) ACTOR_FAMILY="" ;;
esac

consume_attestation() {
  local claim plan_hash rc
  [[ -n "$ACTOR_FAMILY" ]] || return 1
  [[ -f "$MARKER" && ! -L "$MARKER" ]] || return 1
  [[ "$(stat -c '%U:%F:%h:%a' "$MARKER" 2>/dev/null)" == "$(id -un):regular file:1:600" ]] || return 1
  claim="${MARKER}.claim.$$"
  [[ ! -e "$claim" && ! -L "$claim" ]] || return 1
  mv "$MARKER" "$claim" 2>/dev/null || return 1
  plan_hash="$(python3 -c 'import hashlib,sys; text=sys.stdin.read().replace("\r\n","\n").rstrip("\n"); print(hashlib.sha256(text.encode()).hexdigest())' <<<"$PLAN")" || {
    rm -f "$claim"
    return 1
  }
  python3 - "$claim" "$plan_hash" "$ACTOR_FAMILY" "$ACTOR_MODEL" <<'PY'
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

marker = Path(sys.argv[1])
expected_hash, actor_family, actor_model = sys.argv[2:]


def family(model):
    value = model.lower()
    if value.startswith("claude-") or value in {"claude", "opus", "sonnet", "fable", "haiku"}:
        return "A"
    if value.startswith(("gpt-", "gpt-proxy", "codex-cc")):
        return "O"
    return ""


def identity(model):
    value = model.lower()
    for name in ("opus", "sonnet", "fable", "haiku"):
        if name in value:
            return f"A:{name}"
    for name in ("sol", "terra", "luna"):
        if name in value:
            return f"O:{name}"
    return value

try:
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("unsafe marker")
    data = json.loads(marker.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    created = datetime.fromisoformat(data["createdAt"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
    common = (
        data.get("version") == 1
        and data.get("planSha256") == expected_hash
        and data.get("actorFamily") == actor_family
        and actor_family in {"A", "O"}
        and data.get("actorModel") == actor_model
        and family(actor_model) == actor_family
        and data.get("reviewerFamily") in {"A", "O"}
        and isinstance(data.get("reviewerModel"), str)
        and family(data["reviewerModel"]) == data["reviewerFamily"]
        and data.get("reviewerModelSource") in {"requested", "runtime"}
        and (data["reviewerFamily"] != "O" or data["reviewerModelSource"] == "runtime")
        and identity(actor_model) != identity(data["reviewerModel"])
        and data.get("freshContext") is True
        and data.get("sharedSid") is False
        and data.get("sharedTranscript") is False
        and data.get("readOnly") is True
        and isinstance(data.get("reviewRunId"), str)
        and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", data["reviewRunId"], re.I)
        and isinstance(data.get("reviewRequestId"), str)
        and re.fullmatch(r"independent-review-[0-9a-f-]{36}(?:-fallback)?", data["reviewRequestId"], re.I)
        and created <= now + timedelta(seconds=30)
        and created >= now - timedelta(minutes=10)
        and expires > created
        and expires >= now
        and expires <= created + timedelta(minutes=6)
    )
    cross = data.get("reviewMode") == "cross-family" and data["reviewerFamily"] != actor_family and data.get("primaryFailure") is None
    fallback = data.get("reviewMode") == "fresh-context" and data["reviewerFamily"] == actor_family and data.get("primaryFailure") in {"quota", "timeout", "unavailable"}
    raise SystemExit(0 if common and (cross or fallback) else 1)
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY
  rc=$?
  rm -f "$claim"
  return "$rc"
}

if consume_attestation; then
  exit 0
fi

cat >&2 <<EOF
independent-review gate: 고위험 plan(술어: "$MATCHED")의 유효한 일회용 검수 증명이 없습니다.
현재 actor: ${ACTOR_MODEL:-판정 불가}
실행: "$ROOT/libexec/independent-review" --plan-file <plan> --cwd <repo> --transcript <transcript>
EOF
exit 2

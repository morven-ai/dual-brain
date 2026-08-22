#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1

python3 "$ROOT/tests/test_claude_brain.py"
python3 "$ROOT/tests/test_slot_record.py"
node "$ROOT/tests/test_actor.js"
python3 "$ROOT/tests/test_independent_review.py"
python3 "$ROOT/tests/test_installer.py"
python3 "$ROOT/tests/test_paseo_adapter.py"
claude plugin validate "$ROOT" --strict

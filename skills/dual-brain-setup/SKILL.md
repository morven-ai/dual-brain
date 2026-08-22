---
name: dual-brain-setup
description: Linux 또는 WSL2에 Claude Code Dual-Brain을 preflight, plan, apply, verify, rollback할 때 사용합니다.
version: 0.1.0
---

# Dual-Brain Setup

`"${CLAUDE_PLUGIN_ROOT}/bin/dual-brain"`을 사용한다.

1. `preflight`를 실행해 Linux/WSL2, `/proc`, 필수 실행 파일과 충돌을 읽기 전용으로 확인한다.
2. `plan`을 실행한다. 독립 검수 gate는 `--review-gate`, Paseo adapter는 `--paseo <checkout>`을 사용자가 명시했을 때만 포함한다.
3. plan JSON의 변경 경로와 차단 사유를 사용자에게 보여준다.
4. 사용자가 해당 plan 적용을 명시적으로 승인했을 때만 `apply --plan <id>`를 실행한다.
5. `verify`를 실행하고 실패하면 transaction의 자동 rollback 결과까지 보고한다.
6. 수동 복구 요청은 `rollback <transaction-id>`를 사용한다.

`.env`, credential store, transcript 본문을 읽거나 수정하지 않는다. unknown shim/config는 덮어쓰지 않는다. Native Windows와 macOS는 v1 지원 대상으로 안내하지 않는다.

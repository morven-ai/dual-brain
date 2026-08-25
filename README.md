# Claude Code Dual-Brain

같은 Claude Code SID와 transcript를 유지하면서 root brain을 Claude A-pool 또는 GPT O-pool로 교대하는 Linux/WSL2 plugin이다. Native Windows와 macOS는 v1 범위가 아니다.

## 현재 지원 상태

- Ubuntu/Debian 계열 Linux: synthetic E2E와 transaction rollback 지원.
- WSL2: `/proc` 기반 core는 구현됐지만 실제 WSL2 acceptance artifact 전에는 GA 지원으로 간주하지 않는다.
- 실제 A↔O 왕복: 호스트에 기존 Claude·Codex 구독 인증이 있을 때 별도 opt-in acceptance가 필요하다.
- Paseo v0.4.0: provenance(tag·commit·lockfile·patch SHA-256)만 고정하고 patch 본문은 재배포하지 않는다. 획득형 maintenance lease가 없어 자동 patch/cutover는 차단된다.

## 개발 호스트의 구현과의 관계

이 plugin은 이식 가능한 재배포본이고, 개발 호스트의 로컬 bash 구현과 **의도적으로 갈라져 있다.** 두 구현을 동기화하지 않는다.

- 로컬 구현: bash. tmux 슬롯과 호스트 고유 경로에 결합돼 있고, Codex 네이티브 슬롯처럼 이 plugin에 없는 기능을 갖는다.
- 이 plugin: python. preflight·plan·apply·rollback transaction과 config 기반 슬롯 정의를 갖고, 어느 호스트에나 설치된다.

공유하는 것은 코드가 아니라 계약이다 — 슬롯은 얇은 진입점이고, brain 전환은 같은 SID를 유지하며, 검수 게이트는 반대 pool을 강제하고 fail-closed다. 런타임은 각 호스트가 소유한다.

따라서 기본 슬롯 이름(`cc1`~`cc5`, `cx1`~`cx5`)은 이 plugin의 기본값일 뿐이고 로컬 구현과 일치하지 않을 수 있다.

## 요구 사항

`python3`, `node`, `bash`, `tmux`, `jq`, `flock`, `realpath`, `claude`, `claudex`가 PATH에 있어야 한다. 독립 review gate를 켜려면 `codex-cc`도 필요하다. plugin은 credential을 발급·복사·저장하지 않는다.

## 설치

```bash
claude plugin marketplace add morven-ai/dual-brain
claude plugin install dual-brain@dual-brain
```

설치된 Claude Code 대화에서 `/dual-brain:dual-brain-setup`을 실행하거나 다음 순서를 따른다.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/dual-brain" preflight
"${CLAUDE_PLUGIN_ROOT}/bin/dual-brain" plan [--review-gate]
"${CLAUDE_PLUGIN_ROOT}/bin/dual-brain" apply --plan <plan-id>
"${CLAUDE_PLUGIN_ROOT}/bin/dual-brain" verify
```

`plan`은 충돌과 대상 fingerprint를 기록할 뿐 기존 shim/config를 덮어쓰지 않는다. `apply`는 해당 plan ID를 사용자가 명시적으로 승인한 뒤에만 실행한다. `~/.local/bin`이 PATH에 없다면 shell 설정은 사용자가 별도로 한다. installer는 shell profile을 수정하지 않는다.

기본 slot은 `cc1`~`cc5`, `cx1`~`cx5`다.

```bash
cc-slot cc1
cc-slot cx1 --fresh
brain sol
brain opus
brain fable
brain sonnet
```

`brain`은 managed slot 내부에서만 동작한다. 전환 시 기존 child process group을 종료하고 같은 SID를 `--resume`하며 SessionStart hook의 SID ACK가 일치해야 running 상태가 된다.

## 독립 검수

`--review-gate`를 설치 plan에 명시한 경우 고위험 `ExitPlanMode`에 일회용 attestation을 요구한다.

```bash
"${CLAUDE_PLUGIN_ROOT}/libexec/independent-review" \
  --plan-file <plan.md> --cwd <repo> --transcript <session.jsonl>
```

A actor는 O reviewer, O actor는 native A reviewer를 먼저 사용한다. quota·timeout·provider unavailable이 명시된 경우에만 같은 pool의 새 SID·fresh context·read-only process로 fallback한다. actor 판정 불가와 일반 오류는 fail-closed다.

## 진단과 rollback

```bash
dual-brain doctor
dual-brain verify
dual-brain rollback <transaction-id>
```

`apply` 검증 실패 시 config, managed shim, `current` pointer를 transaction 이전 fingerprint로 자동 복구하고 다시 검증한다. 기존 `.env`, credential store, transcript는 읽거나 수정하거나 backup하지 않는다. 기존 운영 launcher와 Paseo runtime의 전환은 별도 migration transaction이다.

## 보안 경계

- state/config/attestation은 plugin data 아래 mode `0700/0600`으로 저장한다.
- unknown shim/config/symlink와 plan 이후 drift는 overwrite하지 않는다.
- native Claude child에는 proxy/model override 환경을 제거한다.
- reviewer 입력·출력의 URL, local path, email, identifier, token 패턴을 redaction한다.
- 같은 OS 사용자 권한을 장악한 악성 process에 대한 cryptographic boundary는 아니다.

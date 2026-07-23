# 개발 워크플로와 검증

## 변경 전 확인

1. [목표와 범위](../foundation/goal-and-scope.md)에서 요청이 CCIM의 압축·복구·안전·관측 경계에 맞는지 확인한다.
2. 영향을 받는 처리 루프와 context metadata/guard/telemetry 영향을 적는다.
3. 기존 코드와 관련 unit test·fixture를 읽고, 구현 전 기대 상태와 차단 조건을 정한다.
4. provider 또는 write tool schema를 바꾸면 지원 행렬과 fixture를 함께 갱신한다.

## 구현 규칙

- 새 압축 경로는 원문 저장, marker/context metadata, retrieve 경로, diagnostics를 분리하지 않는다.
- source kind별 text 처리와 AST 코드를 같은 heuristic으로 뭉개지 않는다.
- write와 evidence guard는 “모델이 그럴듯하게 답했다”가 아니라 retrieve·session·version 검증에만 의존한다.
- telemetry는 부가 로그가 아니라 기능의 검사 인터페이스다. 새 decision/reason은 구조화 feature flag 또는 안정된 필드로 남긴다.
- live LLM 호출이 없어도 fixture에서 재현할 수 있는 검증을 우선한다.

## 기본 검증 명령

```powershell
uv lock --check
uv run ruff check src tests tools/admin_ui tests/compare
uv run python scripts/check_markdown_links.py
uv run pytest tests/unit -q
uv run pytest tests/integration -m "not integration and not ollama" -q
uv run python -m py_compile src/ccim/middleware/chain.py tools/admin_ui/html.py
uv run python -m ccim.operations dry-run
uv run python tests/compare/check_task2_semantics.py tests/fixtures/task2_golden
git diff --check
```

Windows에서는 위 순서를 `scripts/verify.ps1`로 실행할 수 있다. 실제 Redis/PostgreSQL migration fixture와 SHA image smoke는 `.github/workflows/ci.yml`의 분리된 job에서 실행한다.

변경한 영역에 맞는 좁은 테스트를 먼저 실행한다. 예를 들어 middleware 변경은 `tests/unit/test_middleware_chain.py`, evidence 저장소 변경은 `tests/unit/test_reversibility.py`, Admin UI 변경은 `tests/unit/test_admin_ui_app.py`와 `tests/unit/test_admin_measure.py`를 우선한다.

운영 데이터 계약, budget 또는 report 변경은 `tests/unit/test_operations.py`, `tests/unit/test_operations_repository.py`와 PostgreSQL migration fixture를 우선한다. Roadmap 02의 검증에서는 `python -m ccim.operations dry-run`만 사용하며 외부 provider를 호출하지 않는다.

Provider, ingress, write schema 또는 launcher 변경은 다음 fixture 묶음을 우선한다.

```powershell
uv run pytest tests/unit/test_compatibility.py tests/unit/test_cli.py `
  tests/unit/test_routes.py tests/unit/test_llm_client.py `
  tests/unit/test_middleware_chain.py -q
uv run ccim run --dry-run --json --session fixture -- claude -p "health check"
```

`ccim doctor --json`은 실제 Redis, PostgreSQL, migration ledger, provider model list와 port를 읽기
전용으로 검사한다. 외부 의존성이 없는 CI에서는 mock CLI test를 사용하며 unavailable 상태를
성공으로 대체하지 않는다.

## 수동 운영 확인

- Admin UI: `uv run python tools/admin_server.py`
- A/B measure: `uv run python tests/compare/measure.py --compare <left> <right> --since 120 --verbose`
- 직접 압축 경로: `uv run python tests/compare/direct_test.py --session direct-check`
- 배포 준비 상태: `uv run ccim doctor --json`
- Claude Code 환경 확인: `uv run ccim run --dry-run --json -- claude`

PowerShell에서 한글이 깨져 보이면 파일 인코딩을 변경하지 말고 먼저 콘솔 출력 인코딩을 UTF-8로 설정해 확인한다. 저장소 문서는 UTF-8로 유지한다.

## CI/CD 흐름

### 로컬과 CI

1. 로컬 branch에서 구현하고 가장 좁은 관련 테스트부터 실행한다.
2. branch push와 pull request에서 Ruff, unit test, 외부 LLM 없는 integration, semantic checker, Docker image smoke를 실행한다.
3. CI 결과와 익명화된 test report만 artifact로 보관한다. `.env`, prompt, 원문 code, API key는 artifact에 포함하지 않는다.
4. pull request와 일반 branch push에서는 운영 배포 권한을 사용하지 않는다.

### `master` 자동 배포

1. `master` push의 `CI` workflow가 성공한 정확한 `head_sha`만 별도 delivery workflow가 받는다.
2. GitHub-hosted runner가 image를 한 번 build하고 migration·readiness smoke를 통과한 동일 image를 GHCR SHA tag와 immutable digest로 게시한다.
3. GitHub `production` environment를 통과한 job만 `self-hosted`, `linux`, `x64`, `ccim-production` label의 단일 VM runner에 배정된다.
4. VM은 대상 digest를 pull하고 OCI source revision을 확인한 뒤 Docker Compose migration과 서비스를 갱신한다.
5. `/live`와 `/ready` smoke를 통과하면 정상 digest를 기록한다.
6. 실패하면 직전 정상 digest로 rollback하고 배포 결과를 실패로 남긴다.

배포 workflow는 동시 실행을 1개로 제한하고, 새 배포가 진행 중인 배포의 migration·rollback 구간을 취소하지 않는다. cloud access key와 배포용 SSH private key는 GitHub secret에 저장하지 않는다. VM의 runtime secret과 backup identity는 `/etc/ccim`에만 둔다.

### 예약 운영 검증

예약된 GitHub Actions 또는 수동 `workflow_dispatch`는 production self-hosted runner에서 VM 내부의 일일 canary를 실행한다. gateway를 GitHub-hosted runner에 공개하지 않고 VM의 loopback endpoint를 사용한다. 실제 model 호출은 [GPT-5 mini 일일 운영 검증 계획](../evaluation/daily-gpt5-mini-canary.md)의 사전 예산 검사와 hard stop을 통과해야 한다.

VM bootstrap, runner 보호, backup/restore와 provider별 준비 경계는 [클라우드 중립 단일 VM delivery](single-vm-delivery.md)를 따른다.

## Migration과 상태 계약

```powershell
uv run python -m ccim.migrations check
uv run python -m ccim.migrations apply
```

`apply`는 PostgreSQL advisory transaction lock을 얻고 migration checksum ledger에 없는 파일만 적용한다. 이미 idempotent SQL이 적용된 기존 DB는 다시 실행해 ledger에 채택하며, 적용된 version의 checksum이 달라졌으면 중단한다.

- `/live`: 프로세스 event loop가 응답 가능한지만 확인한다.
- `/ready`: Redis, PostgreSQL, migration, telemetry writer 상태를 확인한다.
- 압축이 활성화된 상태에서 Redis가 없거나 PostgreSQL/migration/telemetry가 준비되지 않으면 `/ready`는 `503 degraded`다.
- shutdown은 제한 시간 동안 telemetry background write를 drain하고 실패·drop·timeout 수를 상태와 log에 남긴다.

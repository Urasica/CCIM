# 운영 데이터 축적 준비 계약

## 목적과 단계 경계

Roadmap 02는 실제 운영 수치를 모으는 단계가 아니다. 외부 LLM과 개인 작업 데이터를 사용하기 전에 run identity, telemetry completeness, 예산 차단, 익명화, retention과 보고 형식을 결정적으로 검증한다.

- 허용: deterministic mock provider, synthetic fixture, simulated UTC ledger, `dry-run`·`dummy` 보고서
- 금지: 외부 provider 호출, 개인 code·prompt 수집, 실제 성과 표기, daily-canary와 personal-production 합산
- 실제 기록 시작: AWS 배포와 host/provider 호환성까지 검증한 뒤 Roadmap 05

## 저장 구조

Migration `003_operational_data_readiness.sql`이 다음 구조를 만든다.

| 구조 | 책임 |
|---|---|
| `operational_runs` | run category, commit/config/deployment, provider/model, A/B mode, retention과 계획 request 수 |
| `operational_request_records` | logical request별 attempt, 성공·실패·skip·retry·incomplete, token·latency·retrieve·guard·semantic 관측값 |
| `operational_daily_token_ledgers` | `00:00 UTC` 기준 model group 사용량과 certainty·source |
| `operational_run_metrics` | run 단위 telemetry completeness와 gross/retrieve/net token 집계 |

`RunMetadata`, `RequestObservation`, `DailyTokenLedger` Python 계약과 PostgreSQL constraint가 같은 category/status 값을 사용한다. PostgreSQL writer는 `OperationalRepository`로 분리하며 gateway의 기존 request logger를 바꾸지 않는다.

## Category와 project 경계

| `run_category` | `project_mode` | Roadmap 02에서의 사용 |
|---|---|---|
| `synthetic-dry-run` | `none` | compression off/on mock fixture |
| `daily-canary` | `shared-synthetic` | 실제 호출 없이 category/report 분리만 검증 |
| `personal-production` | `private-production` | 실제 데이터 없이 category/report 분리만 검증 |

project 이름은 저장하지 않고 안정된 salt를 사용한 SHA-256 hash만 저장한다. synthetic dry-run에는 project hash도 넣지 않는다. category와 project mode가 맞지 않으면 Python validation과 PostgreSQL constraint가 모두 거부한다.

`report_label=actual`과 `actual_data=true`는 함께만 사용할 수 있다. `synthetic-dry-run`은 실제 데이터로 표시할 수 없으며 Roadmap 02 CLI는 `actual` 보고서를 생성하지 않는다.

## 상태와 completeness

Request attempt 상태는 `succeeded`, `failed`, `skipped`, `retry`, `incomplete`다.

- 성공과 실패를 모두 관측 표본으로 보존한다.
- `skipped`와 `incomplete`에는 구조화된 제외 사유가 필수다.
- retry attempt만 있고 terminal record가 없으면 logical request를 완료로 세지 않는다.
- telemetry completeness는 계획된 logical request 중 telemetry가 완전한 terminal record가 있는 비율이다.
- 누락과 incomplete를 0 token 또는 성공으로 바꾸지 않는다.

계산식은 다음과 같다.

```text
gross_saved_tokens_est =
  Σ max(tokens_input_original_est - tokens_input_sent_est, 0)

retrieve_overhead_tokens_est =
  Σ retrieve_overhead_tokens_est

net_saved_tokens_est =
  gross_saved_tokens_est - retrieve_overhead_tokens_est

telemetry_completeness_pct =
  telemetry-complete terminal logical requests / planned_requests * 100
```

token 계산은 두 입력 값이 모두 있는 telemetry-complete attempt만 사용하며 표본 수를 함께 표시한다. provider-reported input/cached input/output과 CCIM 추정값은 별도 필드로 유지한다.

## 예산 preflight

`python -m ccim.operations budget-check`는 provider 호출 전 아래 순서로 차단한다.

1. 실제 호출이 금지된 category
2. daily-canary와 shared-synthetic project 불일치
3. model group 전체 사용량 certainty 부족
4. input 180,000, output 20,000, request envelope 200,000 token 상한
5. run 누적 900,000 token 상한
6. UTC 일일 누적 2,100,000 token hard stop

2,500,000 token과 hard stop의 차이인 400,000 token은 사용하지 않는 안전 여유다. 정확히 hard stop에 도달하는 request는 허용하지만 그 뒤의 새 request는 차단한다. simulated ledger는 provider 전체 사용량을 안다고 주장할 수 없으며 실제 호출에 사용할 수 없다.

## 민감정보와 retention

운영 schema와 artifact에는 prompt, 원문 code, source text/path, API key, authorization, credential 필드가 없다. artifact 검사는 금지 key, 일반적인 provider key 형식, Windows user path와 Unix home path를 거부한다.

`retention-v1` 기본 계약:

| 데이터 | 기본 보존 |
|---|---:|
| Redis evidence | 3,600초 |
| operational telemetry | 90일 |
| CI summary artifact | 14일 |
| persistent evidence backup | private encrypted backup만 허용, CI/report 제외 |

각 run은 `telemetry_expires_at`을 필수로 저장한다. evidence 삭제와 telemetry 만료는 서로 다른 수명 주기이며 동일한 backup 정책으로 묶지 않는다. persistent evidence backup의 실제 암호화·복구 구현은 Roadmap 03에서 검증한다.

## 명령 계약

```powershell
uv run python -m ccim.operations dry-run --json
uv run python -m ccim.operations dry-run --output artifacts/operational-readiness.json
uv run python -m ccim.operations report --window-days 7 --json
uv run python -m ccim.operations report --window-days 30 --json
uv run python -m ccim.operations budget-check `
  --run-category daily-canary `
  --project-mode shared-synthetic `
  --known-daily-tokens 1800000 `
  --current-run-tokens 600000 `
  --expected-input-tokens 180000 `
  --max-output-tokens 20000 `
  --usage-certain `
  --json
```

JSON 출력은 `ok`, `schema_version`, `command`, `data`, `warnings`, `errors`를 고정한다. `budget-check`가 호출을 차단하거나 입력·artifact 검사가 실패하면 non-zero로 종료한다. `dry-run`과 `report`는 네트워크, Redis, PostgreSQL, Docker 또는 AWS에 의존하지 않는다.

## 완료 검증

```powershell
uv run pytest tests/unit/test_operations.py tests/unit/test_operations_repository.py -q
uv run python -m ccim.operations dry-run --output artifacts/operational-readiness.json
uv run python scripts/check_artifact_safety.py artifacts/operational-readiness.json
```

SQLite unit test는 `OperationalRepository`의 run·observation·ledger round-trip을 확인한다. PostgreSQL integration은 migration의 세 table·view를 확인하고 같은 mock record를 저장했을 때 completeness와 gross/retrieve/net 계산이 Python report와 같은지 검사한다.

# 개발 로드맵

## 현재 기준선

구현된 핵심은 API gateway, middleware chain, AST/structured/text-span 압축, Redis context, 선택적 SQLite persistent evidence store, `retrieve_original`, current-turn write guard, evidence guard 인터페이스, telemetry와 Admin UI다.

기존 작업 메모에서 완료로 기록된 P0~P4 evidence 확장은 코드와 테스트로 다시 확인하면서 유지한다. 이 문서는 다음 구현 우선순위를 정하는 기준선이며, 완료 표시는 반드시 검증 명령 또는 trace와 함께 갱신한다.

## 우선순위

### 1. 품질 기준선과 migration 운영성

- lint/test/`git diff --check`을 반복 가능한 기준선으로 유지한다.
- 기존 PostgreSQL에 migration이 적용됐는지 확인·적용하는 안전한 절차와 Admin 상태 표시를 제공한다.
- telemetry 비동기 기록 실패와 shutdown drain 상태를 관측 가능하게 만든다.

완료 증거: migration fixture 또는 check command, Admin 상태 테스트, telemetry 실패/flush 테스트.

### 2. 운영 데이터 계약과 수집 준비

- `synthetic-dry-run`, `daily-canary`, `personal-production` run category와 commit/config/provider/deployment metadata를 정의한다.
- provider usage와 추정치, gross/net saving, latency, retrieve, guard, 품질과 운영 실패를 같은 run에서 연결한다.
- 원문·prompt·절대 경로를 외부 artifact에서 제거하고 telemetry/evidence retention을 분리한다.
- mock provider로 성공·실패·skip·retry·incomplete와 telemetry completeness 계산을 재현한다.
- GPT-5 mini 계열의 하루 250만 무료 공유 token 조건, 210만 hard stop과 40만 안전 여유를 model 호출 없는 경계 테스트로 검증한다.
- 실제 외부 LLM canary와 개인 작업 기록은 이 단계에서 시작하지 않는다.

완료 증거: schema/migration fixture, mock dry-run summary, budget preflight 경계 테스트, category별 dummy report와 artifact safety 검사.

### 3. 단일 AWS VM CI/CD

- 로컬 개발, GitHub Actions CI, ECR image 게시, OIDC/Systems Manager 기반 단일 EC2 자동 배포를 연결한다.
- pull request는 CI만 실행하고 `main` 성공 commit만 immutable digest로 배포한다.
- migration/readiness 실패 시 직전 digest로 rollback하고 결과를 추적한다.
- canary runner와 예약 실행 경로는 mock provider와 simulated ledger로 VM 안에서 dry-run한다.
- 외부 LLM 호출과 실제 token ledger 축적은 호환성 검증까지 완료한 뒤 5단계에서 시작한다.

완료 증거: CI run, image digest, Systems Manager command ID, readiness 결과, rollback/restore 훈련, VM canary dry-run.

### 4. Provider와 write tool 호환성

- OpenAI-compatible provider의 실제 지원 response shape, streaming, usage, tool call 경계를 fixture matrix로 명시한다.
- `Edit`, `MultiEdit`, `Write`와 host별 변형의 schema를 fixture로 만들고, 안전하게 해석하지 못하는 write는 차단 또는 명시적 unsupported 처리한다.

완료 증거: provider/write compatibility 문서, 결정적 변환·guard regression tests.

### 5. 실제 운영 증거와 측정 기반 정책

- 1~4단계 완료 후 synthetic daily canary와 비공유 personal-production 관측을 분리해 시작한다.
- 첫 14일에는 active day, session, request, telemetry completeness와 semantic 품질 gate를 확인한다.
- 30일 동안 token 절감, retrieve overhead, p50/p95 지연, 복구, guard, 실패·rollback·restore를 같은 policy cohort로 집계한다.
- Redis/persistent store 간 reload, TTL, 삭제, 저장량, document version 변화를 Admin UI와 trace에서 명확히 보여준다.
- 문서·로그·메일 span의 heuristic은 사실 보존 fixture를 늘리며 보수적으로 확장한다.
- 충분한 production 표본을 확보한 뒤에만 threshold 변경을 shadow 평가와 canary를 거쳐 승격한다.
- evidence guard를 사용하는 외부 action hook은 자동 실행 없이 draft/확인 단계까지만 설계한다.

완료 증거: 분리된 daily-canary/personal-production 집계, 14일 gate, 익명화된 30일 보고서, reload/version/cleanup fixture, report command와 Admin UI 검사.

## 보류 항목

- RAG/vector search와 범용 지식베이스
- MCP 기반 압축 workflow
- 자동 외부 행동과 자동 PR 생성
- 별도 마이크로서비스 분리
- retrieve loop와 충돌할 수 있는 실시간 upstream chunk relay

이 항목들은 현재 문제를 더 작은 metadata, fixture, CLI/trace, 또는 문서로 해결할 수 없는 경우에만 별도 ADR와 평가 계획을 선행한다.

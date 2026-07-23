# 목표와 범위

## 목표

CCIM은 긴 코딩 작업에서 반복 전송되는 코드, `Read` 결과, ToolResult, 로그와 문서 구간을 줄이되, 필요한 원문 근거를 다시 확인할 수 있게 하는 Anthropic Messages·OpenAI Chat Completions 호환 로컬 API 게이트웨이다.

성공은 다음 네 가지가 함께 성립할 때 판단한다.

- 반복 입력 토큰을 줄인다.
- 압축된 내용이 사실을 왜곡하지 않도록 원문과 구조적 사실을 보존한다.
- 압축된 현재 턴의 파일을 원문 재확인 없이 수정하지 못하게 한다.
- 압축·복구·차단·지연을 요청 단위 telemetry로 설명할 수 있다.

## 대상 사용자와 입력

- 대상 사용자: 긴 코드베이스를 읽고 분석·수정·검증하는 코딩 에이전트와 이를 운영하는 개발자.
- 입력: Anthropic Messages 또는 OpenAI Chat Completions 호환 요청, 코드/로그/문서/메일 성격의 ToolResult, `Read` 결과, 선택적으로 `retrieve_original` 도구 호출.
- 보조 상태: Redis의 hot context, 선택적인 SQLite persistent evidence store, PostgreSQL telemetry.

## 출력 행동

- ingress 요청은 canonical Messages request로 정규화된 뒤 압축·PCFI 검사·원문 복구·write schema 검증·guard·telemetry 단계를 거쳐 upstream LLM으로 전달된다.
- 원문이 필요하면 `retrieve_original`이 context ID와 source metadata를 기준으로 Redis 또는 persistent store에서 복구한다.
- 확인이 필요한 write 또는 evidence 기반 행동은 명시적 차단 사유와 재확인 요구를 반환한다.
- 운영자는 Admin UI와 비교 도구에서 토큰, 복구, guard, evidence 상태를 추적한다.

## 범위

포함:

- AST 기반 코드 압축, 구조화 ToolResult 축약·중복 제거, 보수적인 텍스트 span 압축
- context/evidence metadata, content hash·document version, persistent fallback
- `retrieve_original`, current-turn write guard, evidence guard 인터페이스
- Anthropic Messages·OpenAI Chat ingress, Anthropic·OpenAI·OpenAI-compatible upstream 변환, ingress별 합성 SSE 응답
- PCFI, PostgreSQL/OTel telemetry, Admin UI, fixture 기반 테스트와 측정

제외:

- 벡터 검색이나 범용 RAG, 도메인 지식 질의응답 제품
- CCIM 내부의 원인 분석·메일/티켓 작성·근거 패킷 작성
- 외부 시스템에 자동으로 메일을 보내거나 티켓·PR을 생성하는 행동
- MCP 기반 압축 workflow와 host가 파일을 읽은 뒤 사후 변환하는 구조
- 실시간 upstream chunk relay. 현재 스트리밍은 complete 응답을 합성 SSE로 반환한다.
- OpenAI Responses API, multimodal ingress와 검증되지 않은 patch/write schema

## 완료 기준

개발 슬라이스는 다음을 만족해야 완료로 본다.

1. 어느 처리 루프(압축, 복구, write safety, telemetry, 평가)를 개선하는지 문서화한다.
2. 원문·버전·세션 불일치·차단 사유 중 영향을 받는 안전 조건을 명시한다.
3. 단위 테스트, fixture/semantic check, Admin UI/API smoke, 또는 재현 가능한 측정 중 하나로 결과를 확인한다.
4. telemetry 또는 검사 출력으로 왜 해당 결과가 나왔는지 추적할 수 있다.

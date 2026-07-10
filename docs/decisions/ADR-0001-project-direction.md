# ADR-0001: API 게이트웨이 기반 evidence middleware를 유지한다

- 상태: Accepted
- 날짜: 2026-07-10

## 맥락

CCIM의 문제는 코딩 에이전트 세션에서 큰 도구 결과가 반복 전송되고, 이를 압축한 뒤 원문 근거·수정 안전성·운영 효과를 함께 관리해야 한다는 것이다. 범용 검색 제품이나 agent workflow를 추가하면 입력 치환과 원문 복구라는 핵심 경계가 흐려진다.

## 결정

CCIM을 Anthropic Messages 호환 로컬 API gateway와 모듈형 middleware chain으로 유지한다. Redis hot context, 선택적 SQLite persistent evidence store, PostgreSQL telemetry를 기본 저장 경계로 둔다. 원문 복구는 `retrieve_original`을 통해 수행하고, current-turn write guard와 evidence guard로 원문 확인을 강제한다.

## 결과

긍정적 결과:

- 압축·복구·guard가 하나의 요청 trace 안에서 관측된다.
- Redis TTL/재시작에도 persistent fallback으로 근거 복구 경로를 제공할 수 있다.
- provider·agent host 차이는 API 변환과 write schema fixture로 좁은 경계에서 검증할 수 있다.

의도적으로 하지 않는 일:

- RAG/vector search, 범용 사내 지식베이스, 도메인 답변 에이전트
- 자동 코드 수정, PR·티켓·메일 등 외부 행동 실행
- 검증 가능한 필요성이 생기기 전의 MSA 분리와 실시간 chunk relay

## 재검토 조건

별도 서비스 분리는 event ownership, 독립 확장 요구, 독립 장애 모드가 실제로 생기고 현재 모듈형 monolith로 검증할 수 없을 때만 검토한다. RAG 도입은 원문 context ID와 metadata 검증으로 해결할 수 없는 명시적 검색 요구와 평가 fixture가 있을 때만 별도 ADR로 다룬다.

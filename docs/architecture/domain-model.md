# 도메인 모델

CCIM에서 evidence는 검색용 지식 조각이 아니라 압축된 입력을 다시 확인하기 위한 **원문 근거**다. 용어를 다음처럼 구분한다.

| 모델 | 의미 | 핵심 속성 |
|---|---|---|
| Raw input | 에이전트가 보낸 메시지·ToolResult·`Read` 결과 | session, tool name, source path, content |
| Source document | span의 원본 파일·로그·문서·메일 | `document_id`, `source_uri`, `source_kind`, content hash |
| Evidence span | 원문에서 저장·복구 가능한 최소 구간 | context ID, line range, span type, document version, original text |
| Context record | Redis/persistent store가 보관하는 span 레코드 | session ID, metadata, TTL/저장 위치, mapping |
| Compression result | 치환 후 텍스트와 진단 | marker, saved-token estimate, facts, candidate/skip reason |
| Retrieval request | 모델의 원문 복구 요청 | context ID(s), expected session/version |
| Guard decision | write 또는 evidence 행동의 허용·차단 판단 | action type, required/retrieved IDs, reason, version mismatch |
| Request telemetry | 요청 처리의 관측 레코드 | token counts, latency, feature flags, retrieve/guard stats |

## 정체성과 버전

- `context_id`는 복구 대상 span의 식별자다. marker와 tool 입력은 이 ID를 사용한다.
- `session_id`는 context의 사용 범위를 제한한다. 다른 세션 context는 정상 근거로 취급하지 않는다.
- `document_id`는 같은 자료의 논리적 식별자, `document_hash`는 내용 정규화 뒤의 식별자, `document_version`은 내용 변경을 나타낸다.
- source kind에는 code, log, document, email 같은 입력 성격이 들어간다. 이는 압축 방식과 신뢰되지 않은 입력 처리를 선택하는 metadata다.

## 유효성 상태

CCIM은 DKR처럼 현재성 검색을 수행하지 않지만, 근거 사용 가능성은 판단한다.

| 상태 | 의미 | 처리 |
|---|---|---|
| Available | 같은 session·version의 원문을 hot/persistent store에서 복구 가능 | retrieve 및 검증에 사용 |
| Reloaded | Redis miss 뒤 persistent store에서 복구되어 warm load됨 | 사용 가능하되 reload telemetry 기록 |
| Version mismatch | 기대한 document/context version과 저장된 값이 다름 | evidence guard 차단 후 재확인 요구 |
| Cross-session | 다른 session의 context를 요청함 | 차단 |
| Unavailable | Redis와 persistent store 모두에서 원문을 찾지 못함 | retrieve 실패; 확정적 행동 금지 |

이 상태는 모델의 추론이나 벡터 유사도로 결정하지 않는다. 저장된 metadata와 복구 결과로 결정한다.

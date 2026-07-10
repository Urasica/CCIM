# 근거·복구 정책

파일명은 기존 개발 스킬의 문서 경로와 호환을 위해 유지한다. CCIM은 RAG/vector search를 구현하거나 계획하지 않으며, 이 정책은 압축된 입력의 원문 근거를 언제 사용할 수 있는지 정한다.

## 선택 및 저장 정책

- 토큰 임계값, 메시지 위치, tool type, source kind를 먼저 확인해 압축 후보를 정한다.
- AST 코드, 구조화 출력, 텍스트 span은 서로 다른 보수적 경로로 처리한다.
- marker를 만들 때 원문, session, source path/URI, line range, hash/version, 압축 diagnostics를 context record에 연결한다.
- 유사한 내용이라는 이유로 다른 context나 다른 session의 원문을 재사용하지 않는다.

## 복구 우선순위

1. 같은 session과 예상 version을 만족하는 Redis hot context
2. 같은 identity를 만족하는 persistent evidence store record 후 Redis warm load
3. 둘 다 없으면 명시적 retrieve 실패

Redis TTL은 cache 수명일 뿐, 사실의 유효성 판정이 아니다. persistent retention과 삭제 정책은 별도로 관리하며, persistent store가 비활성화된 경우 Redis miss는 복구 불가 상태다.

## 안전 정책

- 벡터 유사성, LLM의 자신감, marker 존재만으로 원문이 확인됐다고 판단하지 않는다.
- current-turn write는 관련 원문 retrieve와 입력 검증 전에는 허용하지 않는다. 관련 없는 write만 예외적으로 허용될 수 있다.
- evidence guard는 required context 누락, retrieve 누락, cross-session, version mismatch, persistent miss를 차단 사유로 사용한다.
- PCFI는 문서·로그 안의 지시문을 사용자 명령으로 취급하지 않도록 신뢰 경계를 유지한다.
- orphan marker가 응답에 남으면 정상 응답으로 은폐하지 않고 검사·복구 경로와 telemetry를 남긴다.

## 응답과 trace 정책

모든 차단·복구 결과는 context ID, source metadata, 판단 사유를 노출할 수 있어야 한다. telemetry에는 최소한 다음을 남긴다.

- 압축 후보/선택/skip reason과 context IDs
- retrieve 호출 수, cache/persistent hit/miss, reload/warm-load 여부
- guard mode, 차단 여부, reason, required/retrieved/validated context
- 원본/전송/출력 토큰과 단계별 지연

## 금지 사항

- 원문 복구가 불가능한 context로 확정적 수정 또는 근거 기반 행동을 진행하지 않는다.
- version이 달라진 자료를 이전 evidence map으로 조용히 재사용하지 않는다.
- 불확실한 자료를 압축률이나 의미 유사성만으로 정상 근거처럼 승격하지 않는다.

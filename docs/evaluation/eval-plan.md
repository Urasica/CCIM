# 평가 계획

## 목적

평가는 “압축이 됐는가”가 아니라 “원문 근거와 안전 장치를 유지한 채 압축이 작동하는가”를 증명한다. LLM 응답 문장만 평가하지 않고 결정적인 상태 전이와 trace를 먼저 확인한다.

## 평가 축

| 축 | 확인할 행동 | 대표 검증 |
|---|---|---|
| 압축 선택 | 큰 코드/현재 턴 ToolResult만 적절히 선택하고 작은 입력은 스킵 | `test_compressor.py`, `test_middleware_chain.py` |
| 사실 보존 | 반복 함수군의 값·호출·비호출 관계를 만들어내지 않음 | `check_task2_semantics.py` fixture |
| 복구 | Redis hit, persistent fallback, warm load가 metadata와 함께 작동 | `test_reversibility.py` |
| 안전 | retrieve 전 write, 불완전 retrieve, cross-session, version mismatch를 차단 | reversibility/middleware unit tests |
| 입력 안전 | 신뢰되지 않은 입력과 injection pattern을 분리·기록 | `test_pcfi.py`, `fixtures/injection_corpus.py` |
| provider 경계 | 변환, tool call, 오류, SSE 형식이 지원 범위 안에서 일관됨 | LLM/routes/translate tests |
| 운영 관측 | measure/admin 경로가 feature flags와 지표를 표시 | admin measure/UI tests, compare CLI |

## Golden case 형식

새 회귀 fixture는 아래 정보를 갖는다. 형식은 JSON/Python fixture 어느 쪽이든 되지만, 기대값은 결정적이어야 한다.

```json
{
  "case_id": "write-after-current-turn-compression",
  "input": "...",
  "expected_context_ids": ["..."],
  "expected_retrieved_ids": ["..."],
  "expected_guard": {"allowed": false, "reason": "blocked_no_retrieve"},
  "expected_flags": {"compress_any": true},
  "must_not_claim": ["unrelated context was validated"]
}
```

## 필수 회귀 사례

- 더 긴 원문이 압축됐지만 fact manifest가 특정 field 값·호출 관계를 보존하는 경우
- Redis가 비어 있고 persistent store가 같은 hash/version의 원문을 reload하는 경우
- document version이 바뀐 뒤 이전 context로 evidence action을 요청해 차단되는 경우
- 다른 session의 context ID를 retrieve 또는 guard에 넣어 차단되는 경우
- 필요한 context 중 일부만 retrieve한 뒤 write를 시도해 차단되는 경우
- `UNKNOWN`에 해당하는 원문 부재에서 정상 결과를 꾸며내지 않고 retrieve 실패/guard reason을 노출하는 경우

## 지표와 보고

- 입력 토큰 절감률과 요청 지연을 함께 기록한다.
- retrieve cache hit, persistent hit, reload hit/miss, guard block rate를 추적한다.
- semantic checker 통과율, 사실 왜곡/false guard의 회귀 수, context trace coverage를 추적한다.
- benchmark 보고서에는 run/session prefix, fixture, 기간, 원본·전송·출력 토큰을 함께 남긴다. 서로 다른 DB 상태의 결과를 같은 A/B 수치처럼 비교하지 않는다.

## 실행 원칙

변경 범위에 맞는 가장 좁은 테스트부터 실행하고, 압축·복구·guard·UI를 함께 바꾼 경우에만 관련 묶음을 확장한다. 명령은 [개발 워크플로](../development/workflow.md)에 정리한다.

# 요청·복구·판단 흐름

이 문서는 스킬의 event/question loop을 CCIM 요청 처리에 맞춰 정의한다. CCIM의 이벤트는 PR·Issue 변경이 아니라, 한 LLM 요청에서 입력이 압축·저장·복구·검증되는 상태 변화다.

## 요청 처리

```text
1. Agent가 `/v1/messages` 또는 `/v1/chat/completions` 요청을 보낸다.
2. ingress adapter가 session을 정하고 요청을 canonical Messages request로 정규화한다.
3. PCFI가 신뢰되지 않은 입력과 위험 신호를 판정한다.
4. Compress가 후보를 선택한다.
   - 코드: AST + fact manifest
   - 구조화 ToolResult: dedupe/summarize
   - 로그·문서·메일: 보수적 text span
5. 원문과 evidence metadata를 Redis에 저장하고, 선택 시 persistent store에도 저장한다.
6. marker와 retrieve_original 도구를 포함한 요청을 upstream으로 보낸다.
7. upstream이 retrieve_original을 호출하면 원문을 복구해 tool result로 돌려준다.
8. compatibility validator가 provider content와 알려진 write schema를 검사한다.
9. upstream 응답의 write tool use는 current-turn write guard가 검증한다.
10. marker 유출 검사·line remap·telemetry 기록 뒤 ingress별 JSON/SSE 응답을 반환한다.
```

## 복구 흐름

```text
retrieve_original(context_id, expected session/version)
  -> session/version 검증
  -> Redis lookup
  -> miss이면 persistent store lookup
  -> persistent hit이면 Redis warm load
  -> source metadata + original text 반환
  -> retrieve/reload stats telemetry 기록
```

복구 결과에는 context ID뿐 아니라 source kind/URI와 line 범위를 담아 모델과 운영자가 어떤 원문을 다시 본 것인지 알 수 있어야 한다.

## 안전 판단 흐름

- 현재 턴에서 압축된 파일을 수정하려는 write는 관련 context가 모두 retrieve되었는지, `old_string` 또는 source mapping으로 원문 확인이 가능한지 검사한다.
- evidence guard가 적용되는 행동은 required context가 존재하고, 같은 session/version으로 retrieve되었는지 검사한다.
- 세션 교차, version mismatch, 불완전 retrieve, 원문 부재는 "추측해 계속"하는 대신 차단 사유와 재확인 요구로 반환한다.
- 알 수 없는 content block, function arguments 또는 write schema는 조용히 통과시키지 않고 compatibility reason과 함께 차단한다.

## 평가 흐름

개발자는 fixture나 실제 비교 run에서 압축 전후의 출력 사실을 확인하고, telemetry에서 압축 선택·복구·guard·지연을 추적한다. 단순 토큰 절감은 품질 통과와 분리해 기록한다.

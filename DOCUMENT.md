# CCIM v2 설계 문서

이 문서는 다른 세션에서 CCIM v2를 이어서 개발하거나 디버깅할 때 필요한 설계와 운영 정보를 한 번에 읽을 수 있도록 정리한 기준 문서다.

## 1. 목적과 범위

CCIM v2는 코딩 에이전트와 LLM API 사이의 로컬 게이트웨이다. 주 목적은 다음 네 가지다.

1. 긴 코딩 세션에서 반복 전송되는 코드/도구 결과를 압축해 입력 토큰을 줄인다.
2. 압축된 원본을 Redis에 저장하고 필요하면 `retrieve_original`로 복구한다.
3. 현재 턴에서 압축된 파일에 대해 안전하지 않은 write tool use를 guard한다.
4. 모든 요청의 토큰, 지연, 압축 후보, skip reason, guard 동작을 PostgreSQL과 Admin UI에서 관측한다.

v2는 대화 전체 의미 요약을 기본 기능으로 넣지 않는다. 우선순위는 AST 코드 압축, ToolResult dedupe, 구조화 출력 축약처럼 원본 보존이 가능하고 품질 리스크가 낮은 계층이다.

## 2. 런타임 구성

### 2.1 프로세스

```text
Admin UI process
    tools/admin_server.py
    127.0.0.1:8090
    .env 편집, CCIM child process start/stop/restart, 측정 UI 제공

CCIM Gateway process
    ccim.main:app
    기본 포트는 .env의 CCIM_PORT
    /health, /v1/messages, /v1/models 제공

Redis
    압축 원본 context, ToolResult 원문, line mapping 저장

PostgreSQL
    requests telemetry 저장
```

Admin UI는 CCIM을 하위 프로세스로 실행한다. Windows에서는 job object를 사용해 Admin UI 종료 시 자식 프로세스 트리를 정리하려고 시도한다. 단, 이미 8081 포트를 점유한 외부 CCIM 프로세스는 Admin UI가 소유하지 않으므로 별도 확인이 필요하다.

### 2.2 의존성 준비

```powershell
cd CCIM\v2
docker compose up -d redis postgres
```

Admin UI 실행:

```powershell
uv run python tools/admin_server.py
```

환경에 따라 `uv`가 PATH에 없으면 해당 머신의 `uv.exe` 절대 경로를 사용한다.

## 3. 요청 처리 흐름

### 3.1 Endpoint

`src/ccim/api/routes.py`

- `POST /v1/messages`: Anthropic Messages API 호환 요청을 받는다.
- `GET /v1/models`: upstream model list를 passthrough하거나 설정 기반 fallback을 반환한다.

`x-ccim-session` 헤더가 있으면 session id로 사용한다. 허용 문자는 `[A-Za-z0-9-]`다. 헤더가 없으면 `CCIM_SESSION_PREFIX + uuid4` 형식으로 생성한다. prefix는 marker-safe하게 sanitize된다.

스트리밍 요청(`stream=true`)은 현재 내부적으로 upstream complete 응답을 받은 뒤 Anthropic SSE 형식으로 합성해 반환한다. 실시간 청크 relay는 retrieve loop와 충돌 가능성이 있어 현재 범위 밖이다.

### 3.2 Middleware chain

`src/ccim/main.py`에서 다음 순서로 조립한다.

```text
PCFIMiddleware
CompressMiddleware
ForwardAndInterceptMiddleware
CurrentTurnWriteGuardMiddleware
OrphanMarkerScanMiddleware
WriteRemapMiddleware
TelemetryMiddleware
```

각 stage는 `RequestContext`를 공유한다.

중요한 `RequestContext` 필드:

| 필드 | 의미 |
|---|---|
| `session_id` | Redis key, marker, telemetry prefix의 기준 |
| `request` | 변형 중인 `MessagesRequest` |
| `tokens_input_original` | 압축 전 추정 입력 토큰 |
| `tokens_input_compressed` | 압축 후 추정 입력 토큰 |
| `tokens_output` | upstream 응답 output tokens |
| `retrieve_original_calls` | retrieve loop 호출 횟수 |
| `blocked` / `block_status_code` | PCFI 또는 loop limit 같은 차단 상태 |
| `response_json` | upstream 또는 guard가 만든 최종 응답 |
| `timings_ms` | pcfi/compress/forward 등 단계별 지연 |
| `extras` | context ids, retrieved contexts, feature_flags 등 확장 상태 |

## 4. PCFI

`PCFIMiddleware`는 `ccim.pcfi.compartments.Compartments`로 system, developer/tool, user, retrieved 계층을 분리하고 `PCFIEnforcer`에 넘긴다.

Llama Guard 설정:

- `CCIM_LLAMAGUARD_URL`이 비어 있으면 regex-only 모드다.
- `CCIM_PCFI_SKIP_GUARD_CATEGORIES`는 guard 결과 중 허용할 카테고리 목록이다. 기본값은 코딩 에이전트 작업에서 오탐이 잦은 `S14`.

PCFI가 block하면 체인은 upstream 호출 없이 중단하고 400 응답을 반환한다.

## 5. 압축 설계

### 5.1 후보 선택

`src/ccim/compress/trigger.py`

`select_compression_candidates()`는 다음 값을 기준으로 후보를 고른다.

- `request_tokens`: 전체 요청 입력 추정치
- `CCIM_COMPRESSION_TRIGGER_TOKENS`: 이 값 미만이면 `below_threshold`
- `CCIM_COMPRESSION_TARGET_TOKENS`: total에서 target까지 줄이기 위해 오래된 후보부터 선택
- 최신 user message 이후의 메시지는 history 후보에서 제외
- system message는 제외
- 압축 가능한 content가 없는 message는 제외

대표 skip reason:

| reason | 의미 |
|---|---|
| `below_threshold` | 전체 입력이 trigger 미만 |
| `target_already_met` | 입력이 이미 target 이하라 줄일 필요 없음 |
| `no_eligible_messages` | 최신 user 이전에 후보가 없음 |
| `current_turn_excluded` | 압축 가능한 내용이 최신 user 이후에만 있음 |
| `system_excluded` | 압축 가능한 내용이 system에만 있음 |
| `no_compressible_content` | 후보 메시지는 있으나 압축 가능한 본문 없음 |
| `not_selected` | compressible은 있으나 target reduction 계산상 선택되지 않음 |
| `current_turn_below_threshold` | current-turn 후보는 있으나 current-turn trigger 미만 |

### 5.2 AST 코드 압축

`src/ccim/compress/ast_compressor.py`

지원 언어:

- Python
- Java
- C#

압축 단위:

- Python: `function_definition` body
- Java/C#: `method_declaration`, `constructor_declaration` body

보존되는 것:

- 함수/메서드 시그니처
- 클래스 구조
- import와 타입 정보
- 압축된 본문 위치의 line mapping

치환되는 것:

```text
<<CTX_{session_id}:{context_id}>>
```

각 `CompressedBlock`은 다음 metadata를 가진다.

| 필드 | 의미 |
|---|---|
| `context_id` | session 내부 context id |
| `marker` | 모델에게 전달되는 마커 |
| `original_code` | Redis에 저장될 원본 본문 |
| `original_lines` | 원본 파일 기준 1-based line range |
| `marker_line` | 압축본에서 marker가 있는 line |
| `symbol_name` | parent function/method 이름 |
| `fact_manifest` | 압축본에 남긴 구조적 사실 목록 |

`CCIM_COMPRESSION_CLUSTER_SUMMARY_ENABLED=true`이면 연속 반복 함수군을 하나의 context로 묶는다. `large_reference.py`의 `transform_batch_001..040` 같은 fixture에서 context 수와 토큰을 크게 줄인다. 단, 한 context가 넓은 원본 범위를 포함하므로 retrieve/write guard UX를 계속 관측해야 한다.

#### Python AST fact manifest

압축된 본문을 마커 하나만으로 대체하면 모델이 함수 사이의 관계를 추측할 수 있다. 그래서 Python 압축은 마커 직전에 `# CCIM fact:` 주석을 추가한다.

예:

```text
# CCIM fact: transform_batch_001: writes=payload['batch']=1, payload['offset']=offset, ...
# CCIM fact: transform_batch_040: writes=payload['batch']=40, payload['offset']=offset, ...
# CCIM fact: run_all: relationship=calls transform_batch_001 from transform_batch_001..transform_batch_040
# CCIM fact: run_all: does_not_call=transform_batch_040
```

생성 위치:

- 일반 함수 압축: 해당 함수 body marker 직전
- 반복 함수군 cluster 압축: cluster marker 직전
- Java/C# 압축: 현재 fact manifest 미적용

추출하는 fact:

| fact | 생성 규칙 |
|---|---|
| `signature` | `ast.FunctionDef` 또는 `ast.AsyncFunctionDef`의 이름, 인자 annotation, return annotation |
| `calls` | 함수 내부 `ast.Call`의 직접 호출명과 attribute 호출명 |
| `writes` | `payload['batch']=40` 같은 subscript assignment |
| `reads` | `raw.get('score')`, `item.payload.get('status')` 같은 주요 읽기 접근 |
| `relationship` | 숫자 suffix 함수군 중 일부만 호출하는 함수가 있을 때 호출한 함수군 범위를 명시 |
| `does_not_call` | 숫자 suffix 함수군의 first/last 중 호출하지 않은 대표 함수를 명시 |

반복 함수군에서는 전체 40개 함수의 fact를 모두 남기면 압축 효과가 줄어든다. 현재 구현은 cluster 안에서 첫 함수와 마지막 함수만 fact로 남긴다. `transform_batch_001..040`처럼 batch 번호가 의미를 갖는 fixture에서 시작값과 끝값을 보존하기 위한 절충이다.

relationship fact는 전체 Python 모듈을 한 번 파싱해 생성한다. `transform_batch_001`, `transform_batch_002`, ..., `transform_batch_040`처럼 같은 prefix와 숫자 suffix를 가진 함수가 4개 이상 있으면 하나의 함수군으로 본다. 어떤 함수가 이 함수군 전체가 아니라 일부만 직접 호출하면 다음과 같은 fact를 추가한다.

```text
run_all: relationship=calls transform_batch_001 from transform_batch_001..transform_batch_040
run_all: does_not_call=transform_batch_040
```

이 보강은 task2에서 확인된 실패를 줄이기 위한 것이다. 이전 압축 결과에서는 `run_all`이 실제로는 `transform_batch_001`만 호출하는데, 모델이 `transform_batch_040`도 집계 경로에 포함된 것처럼 답하는 경우가 있었다. 관계가 없다는 사실을 명시해 이런 잘못된 연결을 줄인다.

### 5.3 TextBlock 압축

`CompressMiddleware._compress_text()`는 텍스트 안의 fenced code block을 대상으로 한다.

감지 대상:

- 코드 펜스 언어 태그 `python`
- 코드 펜스 언어 태그 `py`
- 코드 펜스 언어 태그 `java`
- 코드 펜스 언어 태그 `csharp`
- 코드 펜스 언어 태그 `c#`
- 코드 펜스 언어 태그 `cs`

store 성공이 확인된 code fence만 치환한다. Redis 저장에 실패하면 원문을 그대로 둬서 고아 마커를 만들지 않는다.

### 5.4 ToolResultBlock 압축

`CompressMiddleware._compress_tool_result()`는 코딩 에이전트의 파일 읽기 결과처럼 code fence 없이 raw code가 들어오는 경우를 처리한다.

흐름:

1. `get_tool_result_text()`로 raw text 추출
2. raw code인지 검사
3. 언어 자동 감지
4. AST 압축
5. Redis에 context 저장
6. ToolResultBlock content를 압축된 텍스트로 교체

raw code가 아니면 다음 보조 압축을 시도한다.

- 동일 세션 ToolResult dedupe: content hash가 이미 저장되어 있으면 짧은 참조로 치환
- 구조화 출력 요약: 테스트 성공/실패, traceback, 명령 출력 등 명확한 구조가 있는 긴 출력만 축약

자유 텍스트, 사용자 지시문, assistant 판단문은 요약 대상으로 삼지 않는다.

### 5.5 Current-turn 압축

기본 history 후보 선택은 최신 user 이후 메시지를 제외한다. 하지만 Claude Code 계열에서는 현재 턴의 `Read` 결과가 최신 user message의 `tool_result`로 들어와 바로 다음 reasoning 요청에서 큰 토큰을 차지한다.

current-turn 압축은 이 문제를 보완한다.

조건:

- `CCIM_CURRENT_TURN_COMPRESSION_ENABLED=true`
- 전체 입력이 `CCIM_CURRENT_TURN_COMPRESSION_TRIGGER_TOKENS` 이상
- 최신 user 이후 message의 `ToolResultBlock`
- 해당 ToolResult의 tool_use name이 `CCIM_CURRENT_TURN_COMPRESSION_READ_TOOLS`에 포함
- content가 압축 가능한 raw code

기본 허용 도구:

```text
Read,Grep,Glob,LS,Search
```

현재 실험에서는 `Read` 중심으로 검증했다. `PowerShell`, `Bash`, `Write` 같은 결과는 rejected 진단에 남고 current-turn 압축 후보가 되지 않는다.

current-turn 압축은 가능한 경우 직전 `tool_use` input의 `file_path` 또는 `path`를 source path로 기록한다. 이 mapping은 write guard가 `src/a.py` 수정과 `src/b.py` context를 구분하는 데 사용한다. source path를 찾지 못한 allowed ToolResult는 `compress_current_turn_missing_source_paths`와 `compress_current_turn_missing_source_path_tool_results`에 남긴다. 압축에는 성공했지만 context와 source path를 연결하지 못한 경우 `ctx.extras["current_turn_context_source_missing_ids"]`에도 기록한다.

## 6. Redis 저장 구조

`src/ccim/reversibility/store.py`

Context key:

```text
ctx:{session_id}:{context_id}
```

Payload:

```json
{
  "original_code": "...",
  "language": "python",
  "line_mapping": {"1": 1},
  "source_path": "src/example.py",
  "symbol_name": "run_all",
  "original_lines": [24, 29],
  "created_at": "..."
}
```

ToolResult key:

```text
tool:{session_id}:{content_hash}
```

Payload:

```json
{
  "content": "...",
  "kind": "tool_result",
  "metadata": {"chars": 54107, "lines": 1692},
  "created_at": "..."
}
```

TTL은 `CCIM_REDIS_TTL_SECONDS`로 제어한다. 기본값은 3600초다.

## 7. retrieve_original

`CCIM_COMPRESSION_ENABLE_RETRIEVE=true`이고 실제 압축 context가 생기면 `CompressMiddleware`가 request에 다음을 추가한다.

1. `retrieve_original` tool definition
2. system hint

`ForwardAndInterceptMiddleware`는 upstream LLM 응답에서 `retrieve_original` tool_use를 찾는다. 발견하면 agent로 전달하지 않고 Redis에서 원본을 조회해 tool_result로 LLM에 다시 보낸다. 최대 loop는 기본 5회다.

성공한 retrieve는 `ctx.extras["retrieved_contexts"]`에 저장된다. current-turn write guard는 이 값을 사용해 write 안전성을 판단한다.

Loop limit을 초과하면 502 error envelope을 반환한다.

## 8. Current-turn write guard

`CurrentTurnWriteGuardMiddleware`

목적은 현재 턴에서 압축된 파일 내용을 충분히 복구하지 않은 상태로 모델이 쓰기 도구를 호출하는 것을 막는 것이다.

guard 대상:

```text
CCIM_COMPRESSION_WRITE_GUARD_TOOLS=Edit,MultiEdit,Write
```

정책:

| tool | 허용 조건 |
|---|---|
| `Edit` | `old_string`이 retrieve된 원본 context 안에 있음 |
| `MultiEdit` | 모든 `old_string`이 retrieve된 원본 context 안에 있음 |
| `Write` | 대상 source path 관련 current-turn context가 모두 retrieve됨 |
| unrelated write | target path가 current-turn source path와 무관하면 허용 |

차단 사유:

| reason | 의미 | 대응 |
|---|---|---|
| `blocked_no_retrieve` | 관련 current-turn context가 아직 retrieve되지 않음 | 안내된 context id를 `retrieve_original`로 복원한 뒤 재시도 |
| `blocked_old_string_missing` | retrieve된 원문 안에 `old_string`이 없음 | 원문을 확인해 정확한 기존 문자열로 다시 수정 |
| `blocked_incomplete_retrieve` | `Write` 대상 파일의 current-turn context 중 일부만 retrieve됨 | 대상 파일 관련 context를 모두 복원 |
| `blocked_source_write_unknown_context` | `Write` 대상 path와 연결된 context를 알 수 없음 | source path 추적을 확인하거나 원문을 명시 복원 |
| `blocked_target_context_unknown` | 여러 current-turn context가 있는데 target path와 context mapping이 불완전함 | target 파일의 원문 context를 확인한 뒤 재시도 |
| `missing_old_string` | `Edit` 입력에 `old_string`이 없거나 비어 있음 | `old_string`을 포함해 재시도 |
| `missing_edits` | `MultiEdit` 입력에 edits가 없거나 비어 있음 | edits를 포함해 재시도 |
| `invalid_edit` | `MultiEdit`의 개별 edit 형식이 잘못됨 | edit 객체 형식을 수정 |
| `unsupported_write_tool` | guard 대상에 포함됐지만 정책이 없는 tool | guard 설정 또는 tool 정책을 확인 |

차단은 transport-level 4xx가 아니다. Claude CLI가 retryable request failure로 오해하지 않도록 200 assistant text로 반환한다.

차단 메시지는 대상 파일과 관련된 context id만 안내하려고 한다. source path mapping이 있으면 `src/a.py` 수정 시 `src/b.py` context까지 요구하지 않는다. 반대로 여러 current-turn context가 있는데 source path mapping이 불완전하면 target 파일을 특정할 수 없으므로 보수적으로 차단한다.

## 9. Orphan marker와 write remap

`OrphanMarkerScanMiddleware`는 응답에 `<<CTX_...>>` 마커가 그대로 노출되는 상황을 검사한다. 필요한 경우 원본 retrieve 경로를 유도하거나 telemetry에 남긴다.

`WriteRemapMiddleware`와 `WriteMapper`는 압축본 line mapping을 원본 line으로 되돌리는 V1 계층이다. current-turn write는 별도 guard 정책이 우선한다. v2의 안전한 기본값은 line remap을 맹신하지 않고 retrieve-gated write를 요구하는 것이다.

## 10. Telemetry

PostgreSQL `requests` 테이블:

| 컬럼 | 의미 |
|---|---|
| `session_id` | 요청 세션 |
| `pcfi_action`, `pcfi_reason` | PCFI 결과 |
| `tokens_input_original` | 압축 전 입력 토큰 |
| `tokens_input_compressed` | 압축 후 입력 토큰 |
| `tokens_output` | upstream output tokens |
| `latency_ms` | 전체 지연 |
| `pcfi_latency_ms` | PCFI 지연 |
| `compress_latency_ms` | 압축 지연 |
| `upstream_latency_ms` | LLM 호출 지연 |
| `retrieve_original_calls` | retrieve loop 횟수 |
| `write_remaps` | write remap 횟수 |
| `feature_flags` | 압축/guard 상세 진단 JSON |
| `version` | CCIM version |

주요 `feature_flags`:

### 압축 선택

| key | 의미 |
|---|---|
| `compress_enabled` | Redis 연결 기반 압축 활성 여부 |
| `compress_skip_reason` | 스킵 사유 |
| `compress_total_tokens` | 후보 선택 기준 total |
| `compress_threshold_tokens` | 전역 trigger |
| `compress_target_tokens` | target |
| `compress_total_messages` | 메시지 수 |
| `compress_last_user_idx` | 최신 user index |
| `compress_eligible_messages` | history 후보 가능 message 수 |
| `compress_compressible_messages` | 압축 가능한 message 수 |
| `compress_selected_messages` | 선택된 history message 수 |
| `compress_no_content_messages` | 후보 중 압축할 내용 없는 message 수 |
| `compress_system_excluded` | system이라 제외된 compressible 수 |
| `compress_current_turn_excluded` | 최신 user 이후라 history에서 제외된 compressible 수 |

전역 압축을 끄는 운영 설정은 `CCIM_COMPRESSION_ENABLED=false`다. 이 값이 false이면 `compress_enabled=false`, `compress_skip_reason=disabled`로 기록되고 `tokens_input_original == tokens_input_compressed`가 되어야 한다.

### 압축 결과

| key | 의미 |
|---|---|
| `compress_candidates` | 최종 압축 후보 수 |
| `compress_candidate_messages` | 실제 압축된 message 수 |
| `compress_ast_blocks` | 생성된 AST context 수 |
| `compress_structured_summaries` | 구조화 출력 요약 수 |
| `compress_tool_result_refs` | ToolResult dedupe reference hit |
| `compress_tool_result_stores` | ToolResult 원문 저장 수 |
| `compress_history_contexts` | history 후보에서 생성된 압축 context 수 |
| `compress_history_candidate_messages` | history 후보 중 실제 압축된 message 수 |
| `compress_any` | 실제 압축 발생 여부 |
| `compress_context_ids` | 생성 context 수 |
| `compress_saved_tokens_est` | 추정 입력 토큰 절감 |

### current-turn 진단

| key | 의미 |
|---|---|
| `current_turn_compression_enabled` | 설정값 |
| `compress_current_turn_threshold_tokens` | current-turn trigger |
| `compress_current_turn_candidates` | current-turn 후보 수 |
| `compress_current_turn_contexts` | current-turn 압축 context 수 |
| `compress_current_turn_allowed_tools` | 허용 도구 목록 |
| `compress_current_turn_tool_results` | 현재 턴 ToolResult 수 |
| `compress_current_turn_allowed_tool_results` | 허용 도구 결과 수 |
| `compress_current_turn_rejected_tool_results` | 비허용 도구 결과 수 |
| `compress_current_turn_compressible_tool_results` | 압축 가능 current-turn 결과 수 |
| `compress_current_turn_raw_chars_max` | current-turn raw 최대 문자 수 |
| `compress_current_turn_raw_lines_max` | current-turn raw 최대 줄 수 |
| `compress_current_turn_matched_tool_names` | 허용 매칭된 도구명 |
| `compress_current_turn_rejected_tool_names` | reject된 도구명 |
| `compress_current_turn_source_paths` | current-turn source path 목록 |
| `compress_current_turn_source_path_results` | source path를 찾은 allowed current-turn ToolResult 수 |
| `compress_current_turn_missing_source_paths` | source path를 찾지 못한 allowed current-turn ToolResult 수 |
| `compress_current_turn_missing_source_path_tool_results` | source path를 찾지 못한 tool_result id |

### ToolResult/Text 실패 진단

| key | 의미 |
|---|---|
| `compress_tool_result_attempts` | ToolResult 압축 시도 수 |
| `compress_tool_result_ast_successes` | ToolResult AST 성공 수 |
| `compress_tool_result_failures` | ToolResult 실패 수 |
| `compress_tool_result_last_fail_reason` | 마지막 실패 사유 |
| `compress_tool_result_detected_languages` | 감지 언어 |
| `compress_tool_result_raw_chars_max` | 최대 raw chars |
| `compress_tool_result_raw_lines_max` | 최대 raw lines |
| `compress_text_attempts` | TextBlock 압축 시도 수 |
| `compress_text_ast_successes` | TextBlock AST 성공 수 |
| `compress_text_failures` | TextBlock 실패 수 |
| `compress_text_last_fail_reason` | 마지막 실패 사유 |
| `compress_text_fence_count` | 감지된 code fence 수 |

### context metadata

| key | 의미 |
|---|---|
| `compress_context_metadata_count` | metadata 있는 context 수 |
| `compress_context_symbol_names` | symbol name 샘플 |
| `compress_context_original_ranges` | source path와 line range 샘플 |

### write guard

| key | 의미 |
|---|---|
| `current_turn_write_guard_blocked` | block 여부 |
| `current_turn_write_guard_mode` | `blocked`, `allowed_unrelated_write`, retrieve 허용 모드 등 |
| `current_turn_write_guard_tool` | 검사한 write tool |
| `current_turn_write_guard_target_path` | 대상 경로 |
| `current_turn_write_guard_block_reason` | 차단 사유 |
| `current_turn_write_guard_required_contexts` | 필요한 context 수 |
| `current_turn_write_guard_retrieved_contexts` | retrieve된 context 수 |
| `current_turn_write_guard_validated_contexts` | old_string 또는 source write 검증을 통과한 context 수 |
| `current_turn_write_guard_validated_context_ids` | 검증 통과 context id |
| `current_turn_write_guard_unknown_source_contexts` | source path mapping이 없어 target path와 연결하지 못한 context 수 |

## 11. Admin UI

진입점:

```text
tools/admin_server.py
tools/admin_ui/app.py
```

주소:

```text
http://127.0.0.1:8090
```

구성:

| 파일 | 역할 |
|---|---|
| `tools/admin_ui/config.py` | root, .env, 허용 env key, 기본값 |
| `tools/admin_ui/settings.py` | .env read/write, uv 경로 탐색 |
| `tools/admin_ui/dependencies.py` | Redis/PostgreSQL/CCIM HTTP 상태 확인 |
| `tools/admin_ui/process.py` | CCIM child process와 log 관리 |
| `tools/admin_ui/measure.py` | PostgreSQL measure query |
| `tools/admin_ui/html.py` | 정적 Admin UI HTML/JS |
| `tools/admin_ui/schemas.py` | API payload schema |

Admin UI 기능:

- `.env` LLM provider/model 설정 읽기/쓰기 (`CCIM_LLM_MODEL`로 Claude Code 요청 모델명을 upstream 모델명으로 치환)
- `.env` 압축 설정 읽기/쓰기
- boolean 설정 토글
- CCIM start/stop/restart
- Redis/PostgreSQL/CCIM HTTP 상태 표시
- 8081 포트 점유 PID 표시
- 최신 CCIM log tail 표시
- Measure summary 카드
- 요청별 원본 입력/압축 후 입력 그래프
- Request details 기본 컬럼과 진단 상세 토글
- 진단 상세의 Guard 컬럼에서 guard mode, reason, target path, 필요/retrieve/검증 context 수 표시
- 진단 상세의 Compression detail에서 history context 수와 current-turn context 수를 분리 표시

Measure Request details 기본 컬럼:

```text
Run, #, Time, Original, Sent, Output, Total, Saved, Latency
```

`진단 상세 표시`를 누르면 다음 컬럼이 추가된다.

```text
Guard, Metadata, Compression detail
```

그래프:

- 원본 입력: 점선
- 압축 후 입력: 실선
- left/right prefix는 색상으로 구분

## 12. 측정 도구

### 12.1 measure.py

```powershell
uv run python tools/compare/measure.py --compare <left-prefix> <right-prefix> --since 120 --verbose
```

주의:

- prefix는 `p1`, `p2`처럼 run마다 다르게 잡는다.
- 기존 기록과 섞이면 전체 절감률이 낮게 보일 수 있다.
- DB 볼륨 초기화는 필수가 아니다. 보통 새 `CCIM_SESSION_PREFIX`면 충분하다.

### 12.2 direct_test.py

```powershell
uv run python tools/compare/direct_test.py --session direct-check
```

기본 fixture는 `tools/compare/reference_pipeline.py`다. 이 fixture는 약 3.7k tokens 수준이므로 기본 `CCIM_COMPRESSION_TRIGGER_TOKENS=8000`에서는 스킵된다.

작은 fixture로 압축을 검증하려면:

```env
CCIM_COMPRESSION_TRIGGER_TOKENS=3000
CCIM_COMPRESSION_TARGET_TOKENS=2000
```

또는 큰 fixture를 사용한다.

```powershell
uv run python tools/compare/direct_test.py --session direct-large --reference tools/compare/large_reference.py
```

`direct_test.py`는 압축 미발동 시 DB의 `feature_flags`를 읽어 `skip reason`, `total/threshold`, `target`, 후보 수를 출력한다.

### 12.3 대형 컨텍스트 테스트

대형 컨텍스트 테스트는 CCIM 성능 설명용 대표 시나리오다.

특징:

- `tools/compare/large_reference.py`를 한 번만 읽는다.
- 이후 여러 단계에서 이미 읽은 코드를 재사용한다.
- 현재 턴 Read 압축과 이후 history 압축 반복을 모두 관측할 수 있다.
- 대형 코드 파일 기반이라 약 14.9k token 절감이 안정적으로 보인다.
- 테스트 산출물은 markdown 파일로 남으므로 결과물 존재 여부를 확인할 수 있다.
- `tools/compare/run_task2.py`는 필요할 때 결과물 생성 경로를 별도로 확인하는 보조 스크립트다.

대표 해석:

```text
current-turn 성공 요청:
ct_read=1 ct_allowed=1 ct_comp=1 ct_cand=1
try=1 ok=1 raw_lines=1692 raw_chars=54107 saved≈14900

이후 history 압축 요청:
cand=1 msg=1 ast=3 saved≈14900
current-turn은 PowerShell/Write라 reject되어도 이전 large_reference ToolResult가 압축됨
```

### 12.4 task2 semantic checker

`tools/compare/check_task2_semantics.py`

대형 컨텍스트 테스트는 markdown 산출물이 만들어졌는지만 보면 부족하다. 압축 후 모델이 원본 코드의 사실을 틀리게 재구성할 수 있기 때문이다. semantic checker는 task2 산출물의 형식과 핵심 의미를 함께 검사한다.

실행:

```powershell
uv run python tools/compare/check_task2_semantics.py tools/compare/workspace/q2
```

checker가 보는 항목:

- 네 개의 task2 산출물 존재 여부
- 각 section의 bullet 수와 `ids:` 형식
- 허용된 identifier만 사용했는지
- `payload['batch']=1`, `payload['batch']=40` 같은 batch-specific fact 보존 여부
- `payload['processed_by']`처럼 fixture에 없는 필드가 생겼는지
- `run_all`이 `transform_batch_001`만 호출하고 `transform_batch_040`은 호출하지 않는 관계가 보존됐는지

최근 실패 유형:

| 실패 유형 | 의미 |
|---|---|
| `payload['batch']`를 문자열로 작성 | 실제 원본은 정수 `1`, `40` |
| `payload['processed_by']` 생성 | fixture에 없는 필드 환각 |
| `run_all`과 `transform_batch_040` 연결 | 실제 `run_all`은 `transform_batch_001`만 호출 |
| `mutates shared state` 생성 | fixture는 공유 상태 mutation을 하지 않음 |

checker는 모델 품질 전체를 평가하는 도구가 아니다. task2 fixture에서 반복 확인된 의미 손실을 빠르게 잡는 회귀 테스트다.

### 12.5 최근 q1/q2 비교 결과

최근 2시간 기준으로 비압축 `q1`과 current-turn 압축 `q2`를 비교했다. 두 실행은 같은 task2 성격의 작업이며 원본 입력 규모가 거의 같다.

`q2`는 압축 후 품질 테스트를 통과했다.

```text
OK tools\compare\workspace\q2
```

요약:

| 항목 | q1 비압축 | q2 current-turn 압축 |
|---|---:|---:|
| 요청 수 | 9 | 9 |
| 원본 입력 | 331,732 | 331,754 |
| 전송 입력 | 331,732 | 259,815 |
| 출력 | 5,550 | 6,115 |
| 전송 합계 | 337,282 | 265,930 |
| 절감 | 0 (0%) | 71,939 (21.7%) |
| 평균 지연 | 8,482 ms | 11,972 ms |

관련 이미지:

```text
img/compare.png
```

해석:

- 원본 입력 규모가 거의 같으므로 A/B 비교로 사용할 수 있다.
- q2는 9개 요청에서 전송 입력을 71,939 tokens 줄였다.
- 절감률은 21.7%다.
- 평균 지연은 8,482 ms에서 11,972 ms로 증가했다.
- 이전 실패였던 함수 관계 오해는 fact manifest와 `does_not_call` 보강 후 q2에서 재현되지 않았다.

## 13. 운영상 주의점

1. `CCIM_COMPRESSION_ENABLED=false`면 Redis가 살아 있어도 모든 압축 계층을 건너뛴다.
2. Redis가 내려가 있으면 compression/retrieve가 비활성화된다.
3. PostgreSQL이 내려가 있으면 telemetry/measure가 비활성화된다.
4. Redis/PostgreSQL을 나중에 켜도 이미 실행 중인 CCIM이 자동으로 기능을 되살리지는 않는다. CCIM 재시작이 필요하다.
5. Admin UI가 `Stopped`라고 표시해도 8081 포트를 다른 프로세스가 점유할 수 있다.
6. `CCIM_COMPRESSION_TRIGGER_TOKENS`와 `CCIM_COMPRESSION_TARGET_TOKENS`는 함께 봐야 한다. trigger만 낮춰도 target이 total보다 높으면 `target_already_met`가 될 수 있다.
7. current-turn 압축을 켤 때는 write guard도 켜는 것을 기본으로 한다.
8. `CCIM_COMPRESSION_CLUSTER_SUMMARY_ENABLED=true`는 실험 플래그다. 성능 설명에는 유용하지만 retrieve 범위 확대를 염두에 둔다.
9. Admin UI HTML은 프로세스에 로드되므로 UI 파일 수정 후에는 Admin UI 재시작이 필요할 수 있다.
10. 압축률만으로 성공 여부를 판단하지 않는다. task2처럼 산출물 품질을 확인할 수 있는 작업은 semantic checker 결과를 함께 본다.
11. 비슷한 이름의 함수군을 압축할 때는 관계 부재가 중요하다. 모델이 같은 prefix의 함수들을 같은 실행 경로로 묶어 생각할 수 있으므로 `does_not_call` fact 유지 여부를 확인한다.

## 14. 검증 명령

자주 실행하는 단위 테스트:

```powershell
uv run pytest tests/unit/test_middleware_chain.py -q
uv run pytest tests/unit/test_compressor.py -q
```

정적 검사 예:

```powershell
uv run ruff check src/ccim/middleware/chain.py tests/unit/test_middleware_chain.py
uv run ruff check tools/admin_ui/html.py tools/compare/direct_test.py
uv run ruff check src/ccim/compress/ast_compressor.py tools/compare/check_task2_semantics.py tests/unit/test_compressor.py
```

문법 검사 예:

```powershell
uv run python -m py_compile src/ccim/middleware/chain.py tools/admin_ui/html.py tools/compare/direct_test.py
uv run python -m py_compile src/ccim/compress/ast_compressor.py tools/compare/check_task2_semantics.py
```

task2 산출물 품질 검사:

```powershell
uv run python tools/compare/check_task2_semantics.py tools/compare/workspace/q2
```

`uv`가 PATH에 없으면 해당 머신의 `uv.exe` 절대 경로를 사용한다.

## 15. 알려진 리스크와 후속 작업

### 리스크

- AST 압축만으로 모든 세션에서 30% 절감을 보장할 수는 없다. 코드 비중이 큰 요청에서 효과가 크고, 로그/대화 비중이 큰 요청에서는 낮다.
- current-turn 압축은 write 품질 리스크가 있어 retrieve-gated guard가 필요하다.
- cluster summary는 context 수를 줄이지만 한 context의 원본 범위가 커진다.
- 구조화 출력 축약은 명확한 로그/테스트 출력에만 적용해야 한다. 자유 텍스트 요약은 품질 리스크가 크다.
- fact manifest는 모델의 오해를 줄이지만 완전히 제거하지 않는다. 특히 "관계가 없다"는 정보가 없는 경우 모델이 이름이 비슷한 함수 사이에 관계를 만들 수 있다.
- task2 semantic checker는 fixture 특화 회귀 검사다. 일반 코드 품질을 모두 증명하지 않는다.

### 후속 작업 후보

- current-turn 후보 탈락 사유를 `ct_noncompressible_reason`처럼 더 세분화한다.
- Admin UI Measure에서 guard/metadata 전용 필터를 추가한다.
- ToolResult dedupe hit/miss와 구조화 출력 요약 효과를 별도 그래프로 분리한다.
- write guard 메시지에서 retrieve해야 할 context를 더 짧고 명확하게 안내한다.
- direct/대형 컨텍스트 테스트 측정 결과를 자동으로 markdown report로 내보낸다.
- relationship fact 추출을 Python 외 언어로 확장한다.
- semantic checker를 task2 외 fixture에도 적용할 수 있도록 rule set을 분리한다.

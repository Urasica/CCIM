# Provider, ingress와 write-tool 호환성 행렬

## 판정 기준

`지원`은 아래 fixture 또는 재현 명령이 request, response, tool, usage, SSE와 오류 reason을
결정적으로 확인한다는 뜻이다. 실제 외부 provider 호출이나 개인 작업 관측은 이 문서의 지원
표시에 포함하지 않으며 Roadmap 05에서 별도로 기록한다.

공통 canonical request는 `ccim.api.schemas.MessagesRequest`다. ingress adapter는 canonicalize만
하고 압축·복구·PCFI·write guard·telemetry chain을 복제하지 않는다.

## Ingress

| Ingress | Request/content | Tool 표현 | Streaming | Usage 반환 | Session | 상태와 증거 |
|---|---|---|---|---|---|---|
| Anthropic Messages `/v1/messages` | text와 Anthropic content block | `tool_use`, `tool_result` | Anthropic complete SSE 합성 | `input_tokens`, `output_tokens` | `X-CCIM-Session` 또는 launcher token | 지원 · `test_routes.py`, `test_middleware_chain.py` |
| OpenAI Chat `/v1/chat/completions` | text/string 또는 text block | function `tool_calls`, role `tool` | OpenAI chunk SSE 합성 + `[DONE]` | prompt/completion/total | `X-CCIM-Session` 또는 launcher token | 지원 · `test_compatibility.py`, `test_routes.py` |
| OpenAI multimodal/unknown block | image/audio와 알 수 없는 block | - | - | - | 동일 | 미지원 · `400 unsupported_schema`, `unsupported_content_block` fixture |
| OpenAI Responses `/v1/responses` | Responses input/output item | Responses tool item | Responses event | Responses usage | 동일 | 미지원 · `501`, `unsupported_responses_api` fixture |

OpenAI Chat ingress가 지원하는 top-level option은 `model`, `messages`, `max_tokens` 또는
`max_completion_tokens`, `stream`, function `tools`, `temperature`, `top_p`, `stop`,
`metadata`다. 다른 field는 조용히 제거하지 않고 오류로 반환한다.

## Upstream provider

| Provider 설정 | Base URL | Request 변환 | Response/tool | Streaming 경계 | Usage | 상태와 증거 |
|---|---|---|---|---|---|---|
| `anthropic` | 기본 또는 `ANTHROPIC_BASE_URL` | canonical passthrough | Anthropic text/tool block | raw client 지원, gateway는 complete SSE 합성 | provider usage와 추정치 분리 | contract 지원 · `test_llm_client.py` |
| `openai` | OpenAI API | Chat Completions로 변환 | text와 function tool call을 canonicalize | OpenAI SSE 변환 fixture, gateway complete SSE 합성 | prompt/completion을 canonical usage로 변환 | contract 지원 · `test_llm_client.py`, `test_compatibility.py` |
| `openai-compatible` | `CCIM_LLM_BASE_URL` 필수 | OpenAI와 동일 | 동일한 strict response contract | 동일 | usage 누락은 unavailable로 분리 | contract 지원 · custom base URL mock fixture |

다음 upstream 상태는 `provider_compatibility_reason`과 함께 `502
unsupported_provider_schema`로 차단한다.

- choices/message 누락
- string 또는 null이 아닌 response content
- function 이외의 tool call
- 비어 있는 tool id/name
- JSON object가 아닌 function arguments
- 알 수 없는 finish reason 또는 잘못된 usage

rate-limit HTTP 오류는 provider HTTP 상태로 전달하며 OpenAI client의 제한된 retry 횟수와
실제 HTTP attempt/bytes를 telemetry에 기록한다.

## Write tool

| Tool/schema | Canonical 판단 | 압축 원문 의존 write | 상태와 증거 |
|---|---|---|---|
| `Edit(file_path, old_string, new_string)` | `edit` | 관련 원문에서 `old_string` 확인 | 지원 |
| `MultiEdit(file_path, edits[])` | `multiedit` | 모든 `old_string` 확인 | 지원 |
| `Write(file_path, content)` | `write` | 대상 context 전체 retrieve 확인 | 지원 |
| `edit_file(path, old_string, new_string)` | `edit` alias | `Edit`와 같은 guard | 지원 |
| `multi_edit(path, edits[])` | `multiedit` alias | `MultiEdit`와 같은 guard | 지원 |
| `write_file(path, content)` | `write` alias | `Write`와 같은 guard | 지원 |
| `apply_patch`, `str_replace_editor` | 안전한 단일 schema 없음 | 실행 전에 차단 | 미지원 |
| unknown field, path 충돌, 누락된 required field | canonicalize 불가 | 실행 전에 차단 | 미지원 |

fixture는 `tests/fixtures/compatibility_matrix.json`이며
`write_compatibility_status`, `write_compatibility_reason`,
`current_turn_write_guard_block_reason`을 검사한다.

## Host launcher

Claude Code는 [공식 LLM gateway 설정](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)의
`ANTHROPIC_BASE_URL`을 사용한다.
`ccim run`은 이 환경과 session launcher token을 child process에만 전달한다.

```powershell
uv run ccim run --dry-run --json --session roadmap-04 -- claude -p "health check"
uv run ccim run --session roadmap-04 -- claude
```

launcher는 parent environment, `.env`, Claude Code 설정 파일을 변경하지 않는다. 출력에는 실제
token 대신 placeholder만 표시한다. CCIM gateway와 upstream credential은 launcher 실행 전에
별도로 준비해야 한다.

## Read-only doctor

```powershell
uv run ccim doctor
uv run ccim doctor --json
uv run ccim doctor --offline --json
```

검사 순서는 configuration, gateway port, Redis ping, PostgreSQL read query, migration ledger
inspection, provider model list, compression readiness다. `--offline`은 외부 의존성을 성공으로
꾸미지 않고 `skipped`로 기록해 non-zero로 종료한다. doctor는 migration apply, table 생성,
provider completion, 설정 저장을 수행하지 않는다.

## 결정적 검증

```powershell
uv run pytest tests/unit/test_compatibility.py tests/unit/test_cli.py `
  tests/unit/test_routes.py tests/unit/test_llm_client.py `
  tests/unit/test_middleware_chain.py -q
uv run ruff check src tests
uv run python scripts/check_markdown_links.py
```

각 지원 표시는 위 테스트 또는 fixture 중 하나와 연결된다. 실제 provider/host 버전, model
snapshot, endpoint와 usage 제공 여부는 Roadmap 05 run metadata에서 별도로 고정한다.

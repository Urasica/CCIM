# ADR-0004: canonical ingress와 호환성 계약

상태: 승인
날짜: 2026-07-23

## 배경

CCIM의 middleware chain은 Anthropic Messages 형태를 내부 request model로 사용한다. 기존에는
`/v1/messages`만 ingress로 제공했지만, OpenAI-compatible coding-agent와 local provider를 같은
압축·복구·guard 경로로 검증하려면 외부 schema와 내부 model의 경계를 명시해야 한다.

provider나 host가 알 수 없는 content block, 잘못된 function arguments 또는 해석할 수 없는
write schema를 보내는 경우 조용히 필드를 버리면 압축 후보와 write safety 판단이 달라질 수 있다.
운영 전에 Redis, PostgreSQL, migration, provider와 port 상태를 변경 없이 확인하고, 실제 host
process에만 endpoint를 설정하는 반복 실행 경로도 필요하다.

## 결정

- middleware chain의 canonical request는 기존 `MessagesRequest`로 유지한다.
- Anthropic Messages `/v1/messages`와 OpenAI Chat Completions
  `/v1/chat/completions`가 같은 canonical request로 수렴한다.
- OpenAI Chat ingress는 text와 function tool schema만 지원한다. 알 수 없는 role, content block,
  field 또는 tool arguments는 stable reason과 `400` 오류로 거부하고 telemetry에 기록한다.
- OpenAI Responses `/v1/responses`는 fixture 계약이 추가되기 전까지 `501`과
  `unsupported_responses_api` reason을 반환한다.
- OpenAI/OpenAI-compatible upstream 응답은 text, function tool call, usage, finish reason을
  엄격히 검증한다. 안전하게 canonicalize할 수 없으면 `unsupported_provider_schema`로 차단한다.
- streaming ingress는 complete response를 각 ingress의 SSE 형식으로 합성한다. 실시간 chunk
  relay는 계속 범위 밖이다.
- write-tool registry는 `Edit`, `MultiEdit`, `Write`와 좁은 alias
  (`edit_file`, `multi_edit`, `write_file`)만 해석한다. `apply_patch`,
  `str_replace_editor`와 불명확한 schema는 실행 전에 차단한다.
- `ccim doctor`는 외부 상태를 읽기만 하고 migration apply, 설정 저장, model completion을
  수행하지 않는다.
- `ccim run --host claude-code -- <command>`는 child process에만 `ANTHROPIC_BASE_URL`과
  local launcher token을 설정한다. parent environment와 host 설정 파일은 바꾸지 않는다.
- launcher token의 `ccim-session-<id>` suffix는 gateway가 session id로 해석하며 upstream
  credential로 전달하지 않는다.

## 결과

- 두 ingress는 압축·복구·guard 구현을 복제하지 않는다.
- 지원 범위가 좁지만 unsupported 동작과 telemetry reason이 결정적이다.
- Claude Code는 설정 파일을 바꾸지 않고 같은 session으로 반복 실행할 수 있다.
- OpenAI multimodal content, Responses API, legacy function call과 복잡한 patch grammar는
  별도 fixture와 안전 정책 없이는 지원하지 않는다.
- provider endpoint와 coding-agent binary의 실제 설치·인증은 사용자가 준비해야 하며 기본
  CI에서는 mock과 fixture만 실행한다.

## 검증

- `tests/fixtures/compatibility_matrix.json`
- `tests/unit/test_compatibility.py`
- `tests/unit/test_routes.py`
- `tests/unit/test_llm_client.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_middleware_chain.py`
- `docs/development/compatibility-matrix.md`

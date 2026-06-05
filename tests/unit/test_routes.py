"""api/routes.py 단위 테스트.

외부 의존성(Redis, PG, 실제 LLM)은 chain stub으로 대체.
FastAPI TestClient를 사용해 HTTP 레이어까지 포함한 검증.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from ccim.middleware.chain import RequestContext

# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────


def _make_app(chain_side_effect: Any = None, *, llm_client: Any = None) -> Any:
    """체인을 stub으로 교체한 FastAPI 앱 반환."""
    from fastapi import FastAPI

    from ccim.api.routes import router

    app = FastAPI()
    app.include_router(router)

    mock_chain = MagicMock()

    if chain_side_effect is not None:
        mock_chain.run = chain_side_effect
    else:
        # 기본: 빈 200 응답
        async def _default_run(ctx: RequestContext) -> None:
            ctx.response_json = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }

        mock_chain.run = _default_run

    app.state.chain = mock_chain
    if llm_client is not None:
        app.state.llm_client = llm_client
    return app


_BASE_BODY = {
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "hi"}],
}


# ──────────────────────────────────────────────────────────────────
# 이슈 6 — loop_limit 오류 타입 보존
# ──────────────────────────────────────────────────────────────────


async def test_blocked_loop_limit_returns_correct_error_type() -> None:
    """ctx.blocked=True + response_json에 loop_limit 오류 → pcfi_block으로 덮어쓰지 않는다."""

    async def _chain_run(ctx: RequestContext) -> None:
        ctx.blocked = True
        ctx.block_status_code = 502
        ctx.response_json = {
            "error": {
                "type": "loop_limit",
                "message": "retrieve_original loop limit (3) exceeded.",
            }
        }

    app = _make_app(_chain_run)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/v1/messages", json=_BASE_BODY)

    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["type"] == "loop_limit", (
        f"expected 'loop_limit', got {body['error']['type']!r}"
    )


async def test_blocked_pcfi_without_error_json_returns_pcfi_block() -> None:
    """ctx.blocked=True + response_json 없음(PCFI 차단) → pcfi_block 반환."""

    async def _chain_run(ctx: RequestContext) -> None:
        ctx.blocked = True
        ctx.block_status_code = 403
        ctx.block_reason = "role_switch detected"
        # response_json 설정 안 함

    app = _make_app(_chain_run)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/v1/messages", json=_BASE_BODY)

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["type"] == "pcfi_block"
    assert "role_switch" in body["error"]["message"]


async def test_blocked_with_non_error_response_json_returns_pcfi_block() -> None:
    """ctx.blocked=True + response_json에 error 키 없음 → pcfi_block 폴백."""

    async def _chain_run(ctx: RequestContext) -> None:
        ctx.blocked = True
        ctx.block_status_code = 403
        ctx.response_json = {"some_other_key": "value"}  # error 키 없음

    app = _make_app(_chain_run)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/v1/messages", json=_BASE_BODY)

    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "pcfi_block"


# ──────────────────────────────────────────────────────────────────
# 이슈 7 — session_id 검증 및 충돌 방지
# ──────────────────────────────────────────────────────────────────


async def test_invalid_session_header_returns_400() -> None:
    """x-ccim-session에 비허용 문자 포함 시 400 반환."""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)

    for bad_header in ["team_a", "team/b", "team.c", "session id", "sess:001"]:
        resp = client.post(
            "/v1/messages",
            json=_BASE_BODY,
            headers={"x-ccim-session": bad_header},
        )
        assert resp.status_code == 400, (
            f"header {bad_header!r}: expected 400, got {resp.status_code}"
        )
        assert resp.json()["error"]["type"] == "invalid_session_id"


async def test_valid_session_header_accepted() -> None:
    """x-ccim-session에 허용 문자만 포함 시 정상 처리."""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)

    for good_header in ["team-a", "TeamA", "session123", "abc-123-XYZ"]:
        resp = client.post(
            "/v1/messages",
            json=_BASE_BODY,
            headers={"x-ccim-session": good_header},
        )
        assert resp.status_code == 200, (
            f"header {good_header!r}: expected 200, got {resp.status_code}"
        )


async def test_stream_response_marks_synthesized_mode() -> None:
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    body = {**_BASE_BODY, "stream": True}

    with client.stream("POST", "/v1/messages", json=body) as resp:
        text = "".join(resp.iter_text())

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    assert resp.headers["x-ccim-stream-mode"] == "synthesized_complete_sse"
    assert "message_start" in text
    assert "message_stop" in text


async def test_auto_session_id_uses_safe_chars_only(monkeypatch: Any) -> None:
    """자동 생성 session_id(prefix+UUID)가 마커 안전 문자([A-Za-z0-9-])만 포함."""
    import re

    import ccim.config as _config_mod
    from ccim.config import Settings

    captured_ids: list[str] = []

    async def _chain_run(ctx: RequestContext) -> None:
        captured_ids.append(ctx.session_id)
        ctx.response_json = {
            "id": "x", "type": "message", "role": "assistant",
            "model": "m", "content": [], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    # prefix에 비안전 문자 포함
    mock_settings = MagicMock(spec=Settings)
    mock_settings.session_prefix = "my_team.prefix/"
    # get_settings는 함수 내부에서 ccim.config로 로컬 임포트되므로 원본 모듈 패치
    monkeypatch.setattr(_config_mod, "get_settings", lambda: mock_settings)

    app = _make_app(_chain_run)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/v1/messages", json=_BASE_BODY)

    assert captured_ids, "session_id가 캡처되지 않았음"
    sid = captured_ids[0]
    assert re.fullmatch(r"[A-Za-z0-9\-]+", sid), (
        f"session_id {sid!r}에 비안전 문자 포함"
    )


async def test_different_prefixes_produce_different_session_ids(monkeypatch: Any) -> None:
    """team_a, team-a, team.a는 sanitize 후 같은 prefix 형태가 되지만
    UUID 부분 덕분에 서로 다른 session_id가 생성된다."""
    import ccim.config as _config_mod
    from ccim.config import Settings

    all_ids: list[str] = []

    for prefix in ["team_a", "team-a", "team.a"]:
        captured: list[str] = []

        async def _run(ctx: RequestContext, _buf: list = captured) -> None:
            _buf.append(ctx.session_id)
            ctx.response_json = {
                "id": "x", "type": "message", "role": "assistant",
                "model": "m", "content": [], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

        mock_settings = MagicMock(spec=Settings)
        mock_settings.session_prefix = prefix
        monkeypatch.setattr(_config_mod, "get_settings", lambda s=mock_settings: s)

        app = _make_app(_run)
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/v1/messages", json=_BASE_BODY)

        if captured:
            all_ids.append(captured[0])

    #  prefix에서 생성된 session_id가 모두 다름 (UUID 부분 덕분에)
    assert len(all_ids) == 3
    assert len(set(all_ids)) == 3, f"session_id 충돌 발생: {all_ids}"


async def test_models_passthrough_from_llm_client() -> None:
    class _Client:
        async def list_models(self) -> dict[str, Any]:
            return {
                "object": "list",
                "data": [{"id": "gpt-4o-mini", "object": "model"}],
            }

    app = _make_app(llm_client=_Client())
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/v1/models")

    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "gpt-4o-mini"


async def test_models_fallback_uses_override(monkeypatch: Any) -> None:
    import ccim.config as _config_mod
    from ccim.config import Settings

    mock_settings = MagicMock(spec=Settings)
    mock_settings.llm_model = "gpt-4o-mini-override"
    mock_settings.llm_provider = "openai-compatible"
    monkeypatch.setattr(_config_mod, "get_settings", lambda: mock_settings)

    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/v1/models")

    assert resp.status_code == 200
    assert resp.json()["data"] == [{"id": "gpt-4o-mini-override", "object": "model"}]

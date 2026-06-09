"""고아 마커 방지 — CompressMiddleware + OrphanMarkerScanMiddleware 단위 테스트."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccim.api.schemas import Message, MessagesRequest, TextBlock
from ccim.middleware.chain import (
    CompressMiddleware,
    OrphanMarkerScanMiddleware,
    RequestContext,
)

# ── 픽스처 ────────────────────────────────────────────────────────────

def _make_ctx(text: str) -> RequestContext:
    msg = Message(role="user", content=[TextBlock(text=text)])
    req = MessagesRequest(model="claude-3-5-sonnet-20241022", messages=[msg], max_tokens=1024)
    return RequestContext(session_id="test-session", request=req)


def _make_store(*, put_raises: bool = False, get_returns=None):
    store = MagicMock()
    if put_raises:
        store.put = AsyncMock(side_effect=Exception("Redis down"))
    else:
        store.put = AsyncMock()
    store.get = AsyncMock(return_value=get_returns)
    return store


def _make_settings(*, threshold: int = 100, target: int = 50, enable_retrieve: bool = False):
    s = MagicMock()
    s.compression_trigger_tokens = threshold
    s.compression_target_tokens = target
    s.compression_enable_retrieve = enable_retrieve
    return s


# ── Layer 1: store 실패 시 마커 삽입 안 됨 ────────────────────────────


@pytest.mark.asyncio
async def test_compress_text_store_failure_keeps_original():
    """store.put이 실패하면 펜스가 원본 그대로 유지된다."""
    from ccim.compress.ast_compressor import ASTCompressor

    code_lines = "\n".join(f"    x{i} = {i}" for i in range(10))
    fence_text = f"```python\ndef foo():\n{code_lines}\n```"

    store = _make_store(put_raises=True)
    mw = CompressMiddleware(
        compressor=ASTCompressor(),
        store=store,
        settings=_make_settings(threshold=0),   # threshold=0 → 항상 후보
    )

    new_text, did, _ = await mw._compress_text(fence_text, "sess")

    assert not did, "store 실패 시 did_compress=False 여야 한다"
    assert "<<CTX_" not in new_text, "고아 마커가 삽입되면 안 된다"
    assert "def foo" in new_text, "원본 코드가 보존되어야 한다"


@pytest.mark.asyncio
async def test_compress_text_store_success_inserts_marker():
    """store.put 성공 시 마커가 삽입된다."""
    from ccim.compress.ast_compressor import ASTCompressor

    code_lines = "\n".join(f"    x{i} = {i}" for i in range(10))
    fence_text = f"```python\ndef foo():\n{code_lines}\n```"

    store = _make_store(put_raises=False)
    mw = CompressMiddleware(
        compressor=ASTCompressor(),
        store=store,
        settings=_make_settings(threshold=0),
    )

    new_text, did, _ = await mw._compress_text(fence_text, "sess")

    assert did
    assert "<<CTX_" in new_text
    store.put.assert_awaited()


@pytest.mark.asyncio
async def test_compress_text_partial_store_failure():
    """두 펜스 중 첫 번째 store만 실패하면 두 번째만 압축된다."""
    from ccim.compress.ast_compressor import ASTCompressor

    code_lines = "\n".join(f"    x{i} = {i}" for i in range(10))
    fence = f"```python\ndef foo():\n{code_lines}\n```"
    text = f"{fence}\n\nsome text\n\n{fence}"

    # 첫 호출 실패, 두 번째 성공
    store = MagicMock()
    store.put = AsyncMock(side_effect=[Exception("fail"), None])

    mw = CompressMiddleware(
        compressor=ASTCompressor(),
        store=store,
        settings=_make_settings(threshold=0),
    )

    new_text, did, _ = await mw._compress_text(text, "sess")

    assert did
    # 정확히 마커 1개 (두 번째 펜스만 압축)
    assert new_text.count("<<CTX_") == 1


# ── Layer 3: OrphanMarkerScanMiddleware ───────────────────────────────


@pytest.mark.asyncio
async def test_orphan_scan_restores_from_redis():
    """응답 텍스트의 마커를 Redis에서 원본으로 복원한다."""
    from ccim.reversibility.store import ContextRecord

    record = ContextRecord(
        session_id="test-session",
        context_id="001",
        original_code="    x = 1\n    return x",
        language="python",
        line_mapping={},
    )
    store = _make_store(get_returns=record)

    ctx = _make_ctx("hello")
    ctx.response_json = {
        "content": [{"type": "text", "text": "See code: <<CTX_test-session:001>>"}]
    }

    mw = OrphanMarkerScanMiddleware(store=store)
    await mw(ctx, AsyncMock())

    text = ctx.response_json["content"][0]["text"]
    assert "<<CTX_" not in text
    assert "x = 1" in text


@pytest.mark.asyncio
async def test_orphan_scan_redis_miss_shows_error():
    """Redis miss 시 [복원 불가] 표시로 대체한다."""
    store = _make_store(get_returns=None)

    ctx = _make_ctx("hello")
    ctx.response_json = {
        "content": [{"type": "text", "text": "<<CTX_dead-session:001>>"}]
    }

    mw = OrphanMarkerScanMiddleware(store=store)
    await mw(ctx, AsyncMock())

    text = ctx.response_json["content"][0]["text"]
    assert "<<CTX_" not in text
    assert "restore failed" in text or "복원 불가" in text


@pytest.mark.asyncio
async def test_orphan_scan_redis_exception_shows_error():
    """store.get 예외 시에도 [복원 불가]로 안전하게 처리한다."""
    store = MagicMock()
    store.get = AsyncMock(side_effect=Exception("Redis timeout"))

    ctx = _make_ctx("hello")
    ctx.response_json = {
        "content": [{"type": "text", "text": "<<CTX_err-session:001>>"}]
    }

    mw = OrphanMarkerScanMiddleware(store=store)
    await mw(ctx, AsyncMock())

    text = ctx.response_json["content"][0]["text"]
    assert "<<CTX_" not in text


@pytest.mark.asyncio
async def test_orphan_scan_no_markers_unchanged():
    """마커가 없는 응답은 수정하지 않는다."""
    store = _make_store()

    ctx = _make_ctx("hello")
    original_content = [{"type": "text", "text": "no markers here"}]
    ctx.response_json = {"content": original_content}

    mw = OrphanMarkerScanMiddleware(store=store)
    await mw(ctx, AsyncMock())

    assert ctx.response_json["content"] is original_content
    store.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_orphan_scan_multiple_markers():
    """응답 텍스트 내 여러 마커를 모두 처리한다."""
    from ccim.reversibility.store import ContextRecord

    def make_record(ctx_id: str, code: str):
        return ContextRecord(
            session_id="s",
            context_id=ctx_id,
            original_code=code,
            language="python",
            line_mapping={},
        )

    store = MagicMock()
    store.get = AsyncMock(side_effect=[
        make_record("001", "body_one"),
        make_record("002", "body_two"),
    ])

    ctx = _make_ctx("hello")
    ctx.response_json = {
        "content": [{"type": "text", "text": "A=<<CTX_s:001>> B=<<CTX_s:002>>"}]
    }

    mw = OrphanMarkerScanMiddleware(store=store)
    await mw(ctx, AsyncMock())

    text = ctx.response_json["content"][0]["text"]
    assert "<<CTX_" not in text
    assert "body_one" in text
    assert "body_two" in text


# ── compress_enabled=False (NullRedis 케이스) ─────────────────────────


@pytest.mark.asyncio
async def test_compress_disabled_no_markers_inserted():
    """compress_enabled=False 이면 마커를 삽입하지 않고 원본 메시지를 그대로 전달한다."""
    from ccim.compress.ast_compressor import ASTCompressor

    code_lines = "\n".join(f"    x{i} = {i}" for i in range(10))
    fence_text = f"```python\ndef foo():\n{code_lines}\n```"
    msg = Message(role="user", content=[TextBlock(text=fence_text)])
    req = MessagesRequest(
        model="claude-3-5-sonnet-20241022", messages=[msg], max_tokens=1024
    )
    ctx = RequestContext(session_id="s", request=req)

    store = _make_store()
    mw = CompressMiddleware(
        compressor=ASTCompressor(),
        store=store,
        settings=_make_settings(threshold=0),
        compress_enabled=False,
    )

    forwarded: list[RequestContext] = []

    async def capture(c: RequestContext) -> None:
        forwarded.append(c)

    await mw(ctx, capture)

    assert forwarded, "call_next should have been called"
    forwarded_text = forwarded[0].request.messages[0].content[0].text
    assert "<<CTX_" not in forwarded_text, "disabled 시 마커 삽입 없어야 함"
    assert "def foo" in forwarded_text, "원본 코드가 보존되어야 함"
    store.put.assert_not_awaited()


# ── session_id 충돌 방지 (ctx_prefix nonce) ───────────────────────────


@pytest.mark.asyncio
async def test_concurrent_same_session_no_key_collision():
    """동일 session_id의 두 펜스가 서로 다른 Redis 키에 저장된다."""
    from ccim.compress.ast_compressor import ASTCompressor

    stored_keys: list[str] = []

    store = MagicMock()
    async def capture_put(record):
        stored_keys.append(record.redis_key)
    store.put = capture_put

    code_lines = "\n".join(f"    x{i} = {i}" for i in range(10))
    fence = f"```python\ndef foo():\n{code_lines}\n```"
    # 두 펜스가 같은 텍스트 안에 있음 — 동일 session 내 두 번의 compress() 호출
    text = f"{fence}\n\nsome text\n\n{fence}"

    mw = CompressMiddleware(
        compressor=ASTCompressor(),
        store=store,
        settings=_make_settings(threshold=0),
    )

    _new_text, did, _ = await mw._compress_text(text, "same-session")

    assert did
    assert len(stored_keys) == 2, f"두 블록이 저장되어야 함, got {stored_keys}"
    assert stored_keys[0] != stored_keys[1], (
        f"키가 달라야 함 (충돌 없음): {stored_keys}"
    )


def test_ctx_prefix_produces_unique_context_ids():
    """ctx_prefix가 다르면 같은 session에서도 context_id가 달라진다."""
    from ccim.compress.ast_compressor import ASTCompressor

    code = "def foo():\n" + "\n".join(f"    x{i} = {i}" for i in range(5)) + "\n"
    cmp = ASTCompressor()

    r1 = cmp.compress(code, session_id="s", ctx_prefix="aaa")
    r2 = cmp.compress(code, session_id="s", ctx_prefix="bbb")

    assert r1.blocks[0].context_id != r2.blocks[0].context_id
    assert r1.blocks[0].context_id == "aaa_001"
    assert r2.blocks[0].context_id == "bbb_001"


def test_no_ctx_prefix_keeps_legacy_format():
    """ctx_prefix 없으면 기존 '001' 형식이 유지된다 (하위 호환)."""
    from ccim.compress.ast_compressor import ASTCompressor

    code = "def foo():\n" + "\n".join(f"    x{i} = {i}" for i in range(5)) + "\n"
    result = ASTCompressor().compress(code, session_id="s")

    assert result.blocks[0].context_id == "001"
    assert "<<CTX_s:001>>" in result.compressed_text

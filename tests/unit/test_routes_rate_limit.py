from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ccim.api.routes import router
from ccim.middleware.chain import RequestContext


def _make_app(chain_run):
    app = FastAPI()
    app.include_router(router)

    class DummyChain:
        async def run(self, ctx: RequestContext) -> None:
            await chain_run(ctx)

    app.state.chain = DummyChain()
    return app


async def test_http_429_from_chain_is_returned_as_upstream_429() -> None:
    async def _chain_run(ctx: RequestContext) -> None:
        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        resp = httpx.Response(
            429,
            request=req,
            json={
                "error": {
                    "message": "Rate limit reached. Please try again in 4.224s.",
                    "type": "tokens",
                    "code": "rate_limit_exceeded",
                }
            },
        )
        raise httpx.HTTPStatusError("429 Too Many Requests", request=req, response=resp)

    app = _make_app(_chain_run)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/messages",
        json={"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["type"] == "tokens"
    assert body["error"]["code"] == "rate_limit_exceeded"

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest
from tools.admin_ui import app as admin_app
from tools.admin_ui.html import HTML


class _AdminHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.onclick_handlers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if element_id := attr_map.get("id"):
            self.ids.add(element_id)
        if onclick := attr_map.get("onclick"):
            self.onclick_handlers.append(onclick)


def _parse_admin_html() -> _AdminHtmlParser:
    parser = _AdminHtmlParser()
    parser.feed(HTML)
    parser.close()
    return parser


def _route_paths() -> set[str]:
    return {getattr(route, "path", "") for route in admin_app.app.routes}


@pytest.mark.asyncio
async def test_admin_index_serves_static_html() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<main>" in response.text
    assert 'id="measureSummary"' in response.text
    assert 'id="redisContexts"' in response.text


@pytest.mark.asyncio
async def test_admin_status_requires_token_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_status() -> dict[str, Any]:
        return {"running": False, "dependencies": {}}

    monkeypatch.setenv("CCIM_ADMIN_TOKEN", "secret")
    monkeypatch.setattr(admin_app, "_status", fake_status)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        unauthorized = await client.get("/api/status")
        authorized = await client.get("/api/status", headers={"x-ccim-admin-token": "secret"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json() == {"running": False, "dependencies": {}}


@pytest.mark.asyncio
async def test_admin_measure_data_and_report_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    payload_seen: list[tuple[str, str, int]] = []

    def fake_measure_payload(left: str, right: str, since: int) -> dict[str, Any]:
        payload_seen.append((left, right, since))
        return {
            "since": since,
            "left": {"label": left, "summary": {"requests": 0}, "requests": []},
            "right": {"label": right, "summary": {"requests": 0}, "requests": []},
        }

    def fake_report(data: dict[str, Any]) -> str:
        return f"# Report\n\n{data['left']['label']} vs {data['right']['label']}\n"

    monkeypatch.delenv("CCIM_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(admin_app.measure, "measure_payload", fake_measure_payload)
    monkeypatch.setattr(admin_app.measure, "render_markdown_report", fake_report)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        data_response = await client.post(
            "/api/measure-data",
            json={"left": "base.run", "right": "ccim/run", "since": 45},
        )
        report_response = await client.post(
            "/api/measure-report",
            json={"left": "base.run", "right": "ccim/run", "since": 45},
        )

    assert data_response.status_code == 200
    assert data_response.json()["since"] == 45
    assert report_response.status_code == 200
    assert "text/markdown" in report_response.headers["content-type"]
    assert 'filename="ccim-report-base-run-vs-ccim-run-45m.md"' in report_response.headers[
        "content-disposition"
    ]
    assert payload_seen == [("base.run", "ccim/run", 45), ("base.run", "ccim/run", 45)]


@pytest.mark.asyncio
async def test_admin_redis_contexts_route(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_context_overview() -> dict[str, Any]:
        return {
            "ok": True,
            "url": "redis://localhost:6379/0",
            "session_count": 1,
            "context_count": 2,
            "memory_bytes_est": 128,
            "min_ttl_seconds": 60,
            "sessions": [{"session_id": "s1", "context_count": 2, "contexts": []}],
        }

    monkeypatch.delenv("CCIM_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(admin_app.redis_contexts, "context_overview", fake_context_overview)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/redis-contexts")

    assert response.status_code == 200
    assert response.json()["context_count"] == 2


def test_admin_html_static_dom_references_exist() -> None:
    parser = _parse_admin_html()
    referenced_ids = set(re.findall(r'document\.getElementById\("([A-Za-z0-9_-]+)"\)', HTML))

    assert referenced_ids <= parser.ids
    assert {
        "status",
        "deps",
        "settings",
        "redisSummary",
        "redisContexts",
        "measureSummary",
        "measureChart",
        "measureDetails",
        "ccimLog",
    } <= parser.ids


def test_admin_html_api_references_match_fastapi_routes() -> None:
    referenced_paths = set(re.findall(r'api\("(/api/[A-Za-z0-9_/-]+)"', HTML))
    assert referenced_paths
    assert referenced_paths <= _route_paths()


def test_admin_html_onclick_handlers_have_matching_functions() -> None:
    parser = _parse_admin_html()
    called_functions = {
        match.group(1)
        for onclick in parser.onclick_handlers
        if (match := re.match(r"([A-Za-z_][A-Za-z0-9_]*)\(", onclick.strip()))
    }

    assert called_functions
    for function_name in called_functions:
        assert re.search(rf"\bfunction\s+{re.escape(function_name)}\s*\(", HTML)

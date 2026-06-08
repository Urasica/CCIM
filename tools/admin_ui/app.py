"""FastAPI application for the local admin UI."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from . import dependencies, measure, process, redis_contexts, settings
from .config import HOST, PORT
from .html import HTML
from .schemas import MeasurePayload, SettingsPayload

app = FastAPI(title="CCIM v2 Admin", docs_url=None, redoc_url=None)


def _admin_token() -> str:
    return os.environ.get("CCIM_ADMIN_TOKEN", "").strip()


def _check_token(x_ccim_admin_token: str | None = Header(default=None)) -> None:
    token = _admin_token()
    if token and x_ccim_admin_token != token:
        raise HTTPException(status_code=401, detail="invalid admin token")


def _safe_report_filename_part(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    return safe.strip("-") or "run"


async def _dependency_status() -> dict[str, Any]:
    return await dependencies.dependency_status(process.is_running)


async def _status() -> dict[str, Any]:
    return await process.status(await _dependency_status())


@app.get("/", response_class=HTMLResponse)
async def index(_: Request) -> str:
    return HTML


@app.get("/api/status")
async def status(x_ccim_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(x_ccim_admin_token)
    return await _status()


@app.get("/api/settings")
async def get_settings(x_ccim_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(x_ccim_admin_token)
    return {"values": settings.read_env_values(), "status": await _status()}


@app.post("/api/settings")
async def save_settings(
    payload: SettingsPayload,
    x_ccim_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(x_ccim_admin_token)
    was_running = process.is_running()
    settings.write_env_values(payload.values)
    restarted = False
    if was_running:
        try:
            dependencies.ensure_dependencies_ready(await _dependency_status())
            process.stop_ccim()
            process.start_ccim()
            restarted = True
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=f"settings saved, but automatic restart failed: {exc.detail}",
            ) from exc
    return {
        "values": settings.read_env_values(),
        "status": await _status(),
        "saved": True,
        "restarted": restarted,
    }


@app.post("/api/start")
async def start(x_ccim_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(x_ccim_admin_token)
    dependencies.ensure_dependencies_ready(await _dependency_status())
    process.start_ccim()
    return await _status()


@app.post("/api/stop")
async def stop(x_ccim_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(x_ccim_admin_token)
    process.stop_ccim()
    return await _status()


@app.post("/api/restart")
async def restart(x_ccim_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(x_ccim_admin_token)
    dependencies.ensure_dependencies_ready(await _dependency_status())
    process.stop_ccim()
    process.start_ccim()
    return await _status()


@app.post("/api/measure")
async def run_measure(
    payload: MeasurePayload,
    x_ccim_admin_token: str | None = Header(default=None),
) -> PlainTextResponse:
    _check_token(x_ccim_admin_token)
    output = measure.run_measure_compare(payload.left, payload.right, payload.since, payload.verbose)
    return PlainTextResponse(output, status_code=200)


@app.post("/api/measure-data")
async def measure_data(
    payload: MeasurePayload,
    x_ccim_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(x_ccim_admin_token)
    try:
        return measure.measure_payload(payload.left, payload.right, payload.since)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"measure query failed: {exc}") from exc


@app.post("/api/measure-report")
async def measure_report(
    payload: MeasurePayload,
    x_ccim_admin_token: str | None = Header(default=None),
) -> Response:
    _check_token(x_ccim_admin_token)
    try:
        data = measure.measure_payload(payload.left, payload.right, payload.since)
        markdown = measure.render_markdown_report(data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"measure report failed: {exc}") from exc
    left = _safe_report_filename_part(payload.left)
    right = _safe_report_filename_part(payload.right)
    filename = f"ccim-report-{left}-vs-{right}-{payload.since}m.md"
    return Response(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/redis-contexts")
async def redis_context_overview(
    x_ccim_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(x_ccim_admin_token)
    return await redis_contexts.context_overview()


@app.get("/api/ccim-log")
async def ccim_log(x_ccim_admin_token: str | None = Header(default=None)) -> PlainTextResponse:
    _check_token(x_ccim_admin_token)
    log_path = process.latest_ccim_log_path()
    if log_path is None or not log_path.exists():
        return PlainTextResponse("")
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return PlainTextResponse(text[-12000:])


@app.on_event("shutdown")
async def shutdown() -> None:
    process.stop_ccim()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, reload=False)

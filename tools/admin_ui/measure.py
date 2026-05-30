"""Measure command and telemetry query helpers for the admin UI."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from .config import ROOT
from .settings import effective_env, uv_command


def sync_database_url(env: dict[str, str]) -> str:
    url = env.get(
        "CCIM_DATABASE_URL",
        "postgresql+psycopg://ccim:ccim@localhost:5432/ccim",
    )
    return url.replace("postgresql+psycopg://", "postgresql://", 1).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


def jsonable_request_row(row: dict[str, Any]) -> dict[str, Any]:
    feature_flags = row.get("feature_flags") or {}
    if isinstance(feature_flags, str):
        try:
            import json

            feature_flags = json.loads(feature_flags)
        except ValueError:
            feature_flags = {}
    created_at = row.get("created_at")
    return {
        **row,
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        "feature_flags": feature_flags,
    }


def fetch_measure_requests(prefix: str, since_minutes: int) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    sql = """
        SELECT
            session_id,
            created_at,
            pcfi_action,
            tokens_input_original,
            tokens_input_compressed,
            tokens_output,
            latency_ms,
            compress_latency_ms,
            upstream_latency_ms,
            retrieve_original_calls,
            feature_flags
        FROM requests
        WHERE session_id LIKE %(prefix)s
          AND created_at > NOW() - (INTERVAL '1 minute' * %(mins)s)
        ORDER BY created_at ASC
    """
    with (
        psycopg.connect(sync_database_url(effective_env()), row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, {"prefix": f"{prefix}%", "mins": since_minutes})
        return [jsonable_request_row(dict(row)) for row in cur.fetchall()]


def summarize_measure_requests(rows: list[dict[str, Any]]) -> dict[str, Any]:
    requests = len(rows)
    total_original = sum(row.get("tokens_input_original") or 0 for row in rows)
    total_compressed = sum(row.get("tokens_input_compressed") or 0 for row in rows)
    total_output = sum(row.get("tokens_output") or 0 for row in rows)
    total_sent = total_compressed + total_output
    latencies = [row.get("latency_ms") or 0 for row in rows if row.get("latency_ms") is not None]
    saved = total_original - total_compressed
    return {
        "requests": requests,
        "total_input_original": total_original,
        "total_input_compressed": total_compressed,
        "total_output": total_output,
        "total_tokens_sent": total_sent,
        "saved_input_tokens": saved,
        "saved_input_pct": round(saved / total_original * 100, 1) if total_original else None,
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "retrieve_original_calls": sum(row.get("retrieve_original_calls") or 0 for row in rows),
    }


def run_measure_compare(left: str, right: str, since: int, verbose: bool) -> str:
    cmd = [
        uv_command(),
        "run",
        "python",
        "tools/compare/measure.py",
        "--compare",
        left,
        right,
        "--since",
        str(since),
    ]
    if verbose:
        cmd.append("--verbose")
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env={**os.environ.copy(), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return proc.stdout + proc.stderr

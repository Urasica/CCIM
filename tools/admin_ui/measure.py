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
    flags = [row.get("feature_flags") or {} for row in rows]
    retrieve_result_tokens = sum(
        int(flag.get("retrieve_original_result_tokens_est") or 0) for flag in flags
    )
    retrieve_arg_tokens = sum(
        int(flag.get("retrieve_original_tool_use_tokens_est") or 0) for flag in flags
    )
    retrieve_cache_hits = sum(
        int(flag.get("retrieve_original_cache_hits") or 0) for flag in flags
    )
    retrieve_store_fetches = sum(
        int(flag.get("retrieve_original_store_fetches") or 0) for flag in flags
    )
    evidence_reload_hits = sum(int(flag.get("evidence_reload_hit") or 0) for flag in flags)
    evidence_reload_misses = sum(int(flag.get("evidence_reload_miss") or 0) for flag in flags)
    evidence_persistent_hits = sum(
        int(flag.get("evidence_persistent_store_hit") or 0) for flag in flags
    )
    evidence_persistent_misses = sum(
        int(flag.get("evidence_persistent_store_miss") or 0) for flag in flags
    )
    evidence_redis_warm_loads = sum(
        int(flag.get("evidence_redis_warm_loads") or 0) for flag in flags
    )
    current_turn_guard_blocks = sum(
        1 for flag in flags if flag.get("current_turn_write_guard_blocked") is True
    )
    evidence_guard_blocks = sum(
        1 for flag in flags if flag.get("evidence_guard_blocked") is True
    )
    evidence_guard_version_mismatches = sum(
        int(flag.get("evidence_guard_version_mismatches") or 0) for flag in flags
    )
    guard_blocks = sum(
        1
        for flag in flags
        if flag.get("current_turn_write_guard_blocked") is True
        or flag.get("evidence_guard_blocked") is True
    )
    compressed_requests = sum(1 for flag in flags if int(flag.get("compress_context_ids") or 0) > 0)
    return {
        "requests": requests,
        "total_input_original": total_original,
        "total_input_compressed": total_compressed,
        "total_output": total_output,
        "total_tokens_sent": total_sent,
        "saved_input_tokens": saved,
        "saved_input_pct": round(saved / total_original * 100, 1) if total_original else None,
        "net_saved_input_tokens_est": saved - retrieve_result_tokens - retrieve_arg_tokens,
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "retrieve_original_calls": sum(row.get("retrieve_original_calls") or 0 for row in rows),
        "retrieve_result_tokens_est": retrieve_result_tokens,
        "retrieve_tool_use_tokens_est": retrieve_arg_tokens,
        "retrieve_cache_hits": retrieve_cache_hits,
        "retrieve_store_fetches": retrieve_store_fetches,
        "evidence_reload_hits": evidence_reload_hits,
        "evidence_reload_misses": evidence_reload_misses,
        "evidence_persistent_store_hits": evidence_persistent_hits,
        "evidence_persistent_store_misses": evidence_persistent_misses,
        "evidence_redis_warm_loads": evidence_redis_warm_loads,
        "guard_blocks": guard_blocks,
        "current_turn_guard_blocks": current_turn_guard_blocks,
        "evidence_guard_blocks": evidence_guard_blocks,
        "evidence_guard_version_mismatches": evidence_guard_version_mismatches,
        "compressed_requests": compressed_requests,
    }


def measure_payload(left: str, right: str, since: int) -> dict[str, Any]:
    left_rows = fetch_measure_requests(left, since)
    right_rows = fetch_measure_requests(right, since)
    return {
        "left": {
            "label": left,
            "summary": summarize_measure_requests(left_rows),
            "requests": left_rows,
        },
        "right": {
            "label": right,
            "summary": summarize_measure_requests(right_rows),
            "requests": right_rows,
        },
        "since": since,
    }


def render_markdown_report(data: dict[str, Any]) -> str:
    left = data["left"]
    right = data["right"]
    since = data["since"]
    lines = [
        "# CCIM Benchmark Report",
        "",
        f"- Window: last {since} minutes",
        f"- Left: `{left['label']}`",
        f"- Right: `{right['label']}`",
        "",
        "## Summary",
        "",
        "| Metric | Left | Right | Delta |",
        "|---|---:|---:|---:|",
    ]
    for key, label in [
        ("requests", "Requests"),
        ("total_input_original", "Input original"),
        ("total_input_compressed", "Input sent"),
        ("total_output", "Output"),
        ("total_tokens_sent", "Total sent+output"),
        ("saved_input_tokens", "Input saved"),
        ("net_saved_input_tokens_est", "Net saved est."),
        ("retrieve_original_calls", "Retrieve calls"),
        ("retrieve_result_tokens_est", "Retrieve result tokens est."),
        ("retrieve_cache_hits", "Retrieve cache hits"),
        ("retrieve_store_fetches", "Retrieve store fetches"),
        ("evidence_reload_hits", "Evidence reload hits"),
        ("evidence_reload_misses", "Evidence reload misses"),
        ("evidence_persistent_store_hits", "Persistent store hits"),
        ("evidence_persistent_store_misses", "Persistent store misses"),
        ("evidence_redis_warm_loads", "Redis warm loads"),
        ("guard_blocks", "Guard blocks"),
        ("evidence_guard_blocks", "Evidence guard blocks"),
        ("evidence_guard_version_mismatches", "Evidence version mismatches"),
        ("avg_latency_ms", "Avg latency ms"),
    ]:
        left_value = left["summary"].get(key)
        right_value = right["summary"].get(key)
        lines.append(
            f"| {label} | {_md_num(left_value)} | {_md_num(right_value)} | "
            f"{_md_delta(left_value, right_value)} |"
        )

    lines.extend(
        [
            "",
            "## Request Detail",
            "",
            "| Run | # | Time | Original | Sent | Output | Latency ms | Retrieve | Flags |",
            "|---|---:|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for series in (left, right):
        for idx, row in enumerate(series["requests"], start=1):
            flags = row.get("feature_flags") or {}
            lines.append(
                "| "
                + " | ".join(
                    [
                        _md_escape(series["label"]),
                        str(idx),
                        _md_escape(str(row.get("created_at") or "")[:19]),
                        _md_num(row.get("tokens_input_original")),
                        _md_num(row.get("tokens_input_compressed")),
                        _md_num(row.get("tokens_output")),
                        _md_num(row.get("latency_ms")),
                        _md_num(row.get("retrieve_original_calls")),
                        _md_escape(_compact_flags(flags)),
                    ]
                )
                + " |"
            )
    lines.append("")
    return "\n".join(lines)


def _md_num(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return _md_escape(str(value))


def _md_delta(left: Any, right: Any) -> str:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return "N/A"
    delta = right - left
    sign = "+" if delta > 0 else ""
    if isinstance(delta, float):
        return f"{sign}{delta:,.1f}"
    return f"{sign}{delta:,}"


def _md_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _compact_flags(flags: dict[str, Any]) -> str:
    parts = []
    skip = flags.get("compress_skip_reason")
    if skip:
        parts.append(f"skip={skip}")
    if flags.get("compress_context_ids"):
        parts.append(f"ctx={flags.get('compress_context_ids')}")
    if flags.get("retrieve_original_result_tokens_est"):
        parts.append(f"ret_t={flags.get('retrieve_original_result_tokens_est')}")
    if flags.get("evidence_reload_hit") or flags.get("evidence_reload_miss"):
        parts.append(
            f"reload={flags.get('evidence_reload_hit') or 0}/"
            f"{flags.get('evidence_reload_miss') or 0}"
        )
    if flags.get("current_turn_write_guard_blocked") is True:
        parts.append(f"guard={flags.get('current_turn_write_guard_block_reason') or 'blocked'}")
    if flags.get("evidence_guard_blocked") is True:
        parts.append(f"evidence_guard={flags.get('evidence_guard_block_reason') or 'blocked'}")
    if flags.get("evidence_guard_version_mismatches"):
        parts.append(f"version_mismatch={flags.get('evidence_guard_version_mismatches')}")
    if flags.get("stream_response_mode"):
        parts.append(f"stream={flags.get('stream_response_mode')}")
    return " ".join(parts) or "-"


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

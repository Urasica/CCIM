"""토큰 사용량 비교 측정 스크립트.

사용법:
    python tools/compare/measure.py                    # 최근 2시간 전체 요약
    python tools/compare/measure.py --session baseline  # 특정 세션 프리픽스
    python tools/compare/measure.py --since 30          # 최근 30분
    python tools/compare/measure.py --compare baseline compressed  # 두 세션 비교

PostgreSQL 연결:
    CCIM_DATABASE_URL 환경변수 또는 기본값 사용
    (postgresql+psycopg://ccim:ccim@localhost:5432/ccim)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any


# ─────────────────────────────────────────────────────────────────
# DB 쿼리
# ─────────────────────────────────────────────────────────────────

async def fetch_summary(
    engine: Any,
    *,
    session_prefix: str | None = None,
    since_minutes: int = 120,
) -> list[dict]:
    """세션별 토큰 집계 반환."""
    from sqlalchemy import text

    where_clauses = ["created_at > NOW() - (INTERVAL '1 minute' * :mins)"]
    params: dict[str, Any] = {"mins": since_minutes}

    if session_prefix:
        where_clauses.append("session_id LIKE :prefix")
        params["prefix"] = f"{session_prefix}%"

    where_sql = " AND ".join(where_clauses)

    sql = text(f"""
        SELECT
            session_id,
            COUNT(*)                                  AS requests,
            SUM(tokens_input_original)                AS total_input_original,
            SUM(tokens_input_compressed)              AS total_input_compressed,
            SUM(tokens_output)                        AS total_output,
            ROUND(AVG(latency_ms))                    AS avg_latency_ms,
            SUM(retrieve_original_calls)              AS total_retrieve_calls,
            MIN(created_at)                           AS first_request,
            MAX(created_at)                           AS last_request
        FROM requests
        WHERE {where_sql}
        GROUP BY session_id
        ORDER BY first_request DESC
    """)

    async with engine.connect() as conn:
        result = await conn.execute(sql, params)
        rows = result.mappings().all()
        return [dict(r) for r in rows]


async def fetch_per_request(
    engine: Any,
    session_prefix: str,
    since_minutes: int = 120,
) -> list[dict]:
    """요청별 상세 데이터."""
    from sqlalchemy import text

    sql = text("""
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
        WHERE session_id LIKE :prefix
          AND created_at > NOW() - (INTERVAL '1 minute' * :mins)
        ORDER BY created_at ASC
    """)
    async with engine.connect() as conn:
        result = await conn.execute(
            sql, {"prefix": f"{session_prefix}%", "mins": since_minutes}
        )
        return [dict(r) for r in result.mappings().all()]


# ─────────────────────────────────────────────────────────────────
# 리포트 출력
# ─────────────────────────────────────────────────────────────────

def _savings_pct(original: int | None, compressed: int | None) -> str:
    if not original or not compressed:
        return "N/A"
    saved = original - compressed
    pct = saved / original * 100
    return f"{pct:.1f}% ({saved:,} tokens)"


def print_session_table(rows: list[dict]) -> None:
    if not rows:
        print("  (데이터 없음)")
        return

    header = (
        f"{'세션 ID':<32} {'요청':<6} {'입력(원본)':<12} "
        f"{'입력(압축)':<12} {'출력':<10} {'절감':<20} {'지연(avg)':<10}"
    )
    print(header)
    print("-" * len(header))

    for r in rows:
        orig = r["total_input_original"] or 0
        comp = r["total_input_compressed"] or 0
        saved_str = _savings_pct(orig, comp) if orig != comp else "-"
        print(
            f"{str(r['session_id']):<32} "
            f"{r['requests']:<6} "
            f"{orig:<12,} "
            f"{comp:<12,} "
            f"{r['total_output'] or 0:<10,} "
            f"{saved_str:<20} "
            f"{r['avg_latency_ms'] or 0:<10} ms"
        )


def print_comparison(
    baseline_rows: list[dict],
    compressed_rows: list[dict],
    label_a: str = "baseline",
    label_b: str = "compressed",
) -> None:
    def agg(rows: list[dict]) -> dict:
        return {
            "requests": sum(r["requests"] for r in rows),
            "input_original": sum((r["total_input_original"] or 0) for r in rows),
            "input_compressed": sum((r["total_input_compressed"] or 0) for r in rows),
            "output": sum((r["total_output"] or 0) for r in rows),
            "avg_latency": (
                sum((r["avg_latency_ms"] or 0) for r in rows) / len(rows)
                if rows else 0
            ),
            "retrieve_calls": sum((r["total_retrieve_calls"] or 0) for r in rows),
        }

    a = agg(baseline_rows)
    b = agg(compressed_rows)

    print("\n" + "=" * 60)
    print(f"  토큰 사용량 비교: {label_a}  vs  {label_b}")
    print("=" * 60)

    def row(label: str, va: Any, vb: Any, unit: str = "", invert: bool = False) -> None:
        if isinstance(va, float):
            sa, sb = f"{va:.1f}{unit}", f"{vb:.1f}{unit}"
        else:
            sa, sb = f"{va:,}{unit}", f"{vb:,}{unit}"
        # 변화율
        if isinstance(va, (int, float)) and va > 0:
            delta = (vb - va) / va * 100
            arrow = "down" if delta < 0 else "up"
            delta_str = f"  {arrow} {abs(delta):.1f}%"
        else:
            delta_str = ""
        print(f"  {label:<28} {sa:<16} {sb:<16}{delta_str}")

    print(f"\n  {'항목':<28} {label_a:<16} {label_b:<16} 변화")
    print(f"  {'-'*28} {'-'*16} {'-'*16} {'------'}")
    row("요청 수",               a["requests"],        b["requests"])
    row("입력 토큰 (원본)",       a["input_original"],  b["input_original"],  " t")
    row("입력 토큰 (전송)",       a["input_compressed"],b["input_compressed"], " t")
    row("출력 토큰",              a["output"],          b["output"],          " t")
    row("총 토큰",
        a["input_compressed"] + a["output"],
        b["input_compressed"] + b["output"], " t")
    row("평균 지연",              a["avg_latency"],     b["avg_latency"],     " ms")
    row("retrieve_original 호출", a["retrieve_calls"],  b["retrieve_calls"])

    # 핵심 절감 수치
    orig_a = a["input_original"]
    comp_b = b["input_compressed"]
    if orig_a > 0 and comp_b > 0:
        saved_pct = (orig_a - comp_b) / orig_a * 100
        saved_abs = orig_a - comp_b
        goal_met = "OK 목표 달성 (30%+)" if saved_pct >= 30 else "NO 목표 미달 (< 30%)"
        print(f"\n  {'-'*58}")
        print(f"  입력 토큰 절감: {saved_abs:,} tokens  ({saved_pct:.1f}%)  {goal_met}")

    print("=" * 60 + "\n")


def print_per_request_table(rows: list[dict], label: str) -> None:
    print(f"\n  [{label}] 요청별 상세")
    print(
        f"  {'시각':<20} {'원본t':<8} {'압축t':<8} {'출력t':<8} "
        f"{'지연ms':<8} {'pcfi':<6} {'압축상세'}"
    )
    print(f"  {'-'*20} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*6} {'-'*34}")
    for r in rows:
        ts = str(r["created_at"])[:19] if r["created_at"] else "?"
        flags = r.get("feature_flags") or {}
        detail = _format_compress_flags(flags)
        print(
            f"  {ts:<20} "
            f"{(r['tokens_input_original'] or 0):<8,} "
            f"{(r['tokens_input_compressed'] or 0):<8,} "
            f"{(r['tokens_output'] or 0):<8,} "
            f"{(r['latency_ms'] or 0):<8} "
            f"{(r['pcfi_action'] or '?'):<6} "
            f"{detail}"
        )


def _format_compress_flags(flags: dict[str, Any]) -> str:
    if not flags:
        return "-"
    if flags.get("compress_enabled") is False:
        return "disabled"
    skip = flags.get("compress_skip_reason")
    if skip:
        return (
            f"skip={skip} "
            f"elig={flags.get('compress_eligible_messages', 0)} "
            f"comp={flags.get('compress_compressible_messages', 0)} "
            f"cur={flags.get('compress_current_turn_excluded', 0)} "
            f"sys={flags.get('compress_system_excluded', 0)} "
            f"empty={flags.get('compress_no_content_messages', 0)}"
        )
    parts = [
        f"cand={flags.get('compress_candidates', 0)}",
        f"msg={flags.get('compress_candidate_messages', 0)}",
        f"elig={flags.get('compress_eligible_messages', 0)}",
        f"comp={flags.get('compress_compressible_messages', 0)}",
        f"cur={flags.get('compress_current_turn_excluded', 0)}",
        f"ct={flags.get('compress_current_turn_contexts', 0)}",
        f"ast={flags.get('compress_ast_blocks', 0)}",
        f"log={flags.get('compress_structured_summaries', 0)}",
        f"ref={flags.get('compress_tool_result_refs', 0)}",
        f"store={flags.get('compress_tool_result_stores', 0)}",
        f"saved={flags.get('compress_saved_tokens_est', 0)}",
    ]
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    db_url = os.environ.get(
        "CCIM_DATABASE_URL",
        "postgresql+psycopg://ccim:ccim@localhost:5432/ccim",
    )

    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        engine = create_async_engine(db_url, pool_pre_ping=True)
        async with engine.connect() as conn:
            from sqlalchemy import text
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        print(f"[오류] PostgreSQL 연결 실패: {exc}")
        print(f"  URL: {db_url}")
        print("  CCIM_DATABASE_URL 환경변수 또는 .env를 확인하세요.")
        sys.exit(1)

    since = args.since

    if args.compare:
        label_a, label_b = args.compare[0], args.compare[1]
        print(f"\n> 비교 모드: '{label_a}'  vs  '{label_b}'  (최근 {since}분)")

        rows_a = await fetch_summary(engine, session_prefix=label_a, since_minutes=since)
        rows_b = await fetch_summary(engine, session_prefix=label_b, since_minutes=since)

        print(f"\n[{label_a}] 세션 목록:")
        print_session_table(rows_a)

        print(f"\n[{label_b}] 세션 목록:")
        print_session_table(rows_b)

        print_comparison(rows_a, rows_b, label_a, label_b)

        if args.verbose:
            detail_a = await fetch_per_request(engine, label_a, since)
            detail_b = await fetch_per_request(engine, label_b, since)
            print_per_request_table(detail_a, label_a)
            print_per_request_table(detail_b, label_b)

    elif args.session:
        prefix = args.session
        print(f"\n> 세션 조회: '{prefix}'  (최근 {since}분)")
        rows = await fetch_summary(engine, session_prefix=prefix, since_minutes=since)
        print_session_table(rows)

        if args.verbose:
            detail = await fetch_per_request(engine, prefix, since)
            print_per_request_table(detail, prefix)

    else:
        print(f"\n> 전체 세션 요약  (최근 {since}분)")
        rows = await fetch_summary(engine, since_minutes=since)
        print_session_table(rows)

    await engine.dispose()


def cli() -> None:
    import sys
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="CCIM 토큰 사용량 측정")
    parser.add_argument(
        "--session", "-s",
        help="조회할 세션 ID 프리픽스 (예: baseline, compressed)",
    )
    parser.add_argument(
        "--compare", "-c",
        nargs=2,
        metavar=("A", "B"),
        help="두 세션 프리픽스를 비교 (예: --compare baseline compressed)",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=120,
        metavar="MINUTES",
        help="최근 N분 데이터 조회 (기본: 120)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="요청별 상세 테이블 출력",
    )
    args = parser.parse_args()
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()

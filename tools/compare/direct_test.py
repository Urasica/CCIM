"""CCIM ToolResultBlock 압축 직접 검증 스크립트.

Claude Code를 거치지 않고 CCIM API에 직접 요청을 보내,
reference_pipeline.py가 ToolResultBlock에 담긴 실제 대화 구조에서 압축이 발동하는지 확인.

대화 구조 (5개 메시지):
  [0] user      : "reference_pipeline.py 분석 요청"
  [1] assistant : tool_use(Read, reference_pipeline.py)
  [2] user      : tool_result(reference_pipeline.py 전체 내용)   ← 압축 대상
  [3] assistant : "파일 읽었습니다, 분석 시작합니다"
  [4] user      : "RuleEngine 설명해줘"            ← 최신 user 메시지

should_compress: total > threshold → 후보 탐색 (last_user_idx = 4)
  → message[2] (ToolResultBlock, Python 1761줄) → 압축 발동

사용법:
    python tools/compare/direct_test.py
    python tools/compare/direct_test.py --url http://localhost:8081 --session test-001
    python tools/compare/direct_test.py --no-db  # DB 쿼리 없이 API 응답만 출력
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx


def default_model() -> str:
    """Return the configured test model, matching CCIM_LLM_MODEL when available."""
    env_model = os.environ.get("CCIM_LLM_MODEL", "").strip()
    if env_model:
        return env_model

    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "CCIM_LLM_MODEL":
                model = value.strip().strip("\"'")
                if model:
                    return model

    return "gpt-5-mini"

# ─────────────────────────────────────────────────────────────────
# 테스트 실행
# ─────────────────────────────────────────────────────────────────

def build_messages(reference_content: str) -> list[dict]:
    """압축이 발동하는 5-메시지 대화 구조 생성."""
    return [
        # [0] 최초 user 요청
        {
            "role": "user",
            "content": "v1/tools/compare/reference_pipeline.py 파일을 읽고 Pipeline 클래스 구조를 분석해줘."
        },
        # [1] assistant: Read 도구 호출 (Claude Code 패턴 재현)
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_direct_01",
                    "name": "Read",
                    "input": {"file_path": "v1/tools/compare/reference_pipeline.py"}
                }
            ]
        },
        # [2] user: tool_result — reference_pipeline.py 전체 내용 (압축 대상)
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_direct_01",
                    "content": reference_content
                }
            ]
        },
        # [3] assistant: 1차 응답 (파일 확인 메시지)
        {
            "role": "assistant",
            "content": (
                "reference_pipeline.py 파일을 확인했습니다. "
                "Pipeline은 이벤트 정규화, 규칙 평가, 요약 생성을 연결합니다. "
                "자세한 분석을 진행하겠습니다."
            )
        },
        # [4] user: 최신 user 메시지 (last_user_idx = 4 → message[2] 압축 가능)
        {
            "role": "user",
            "content": (
                "ConditionEvaluator와 RuleEngine의 책임을 설명하고, "
                "새 연산자를 추가해야 하는 위치를 찾아줘."
            )
        },
    ]


def send_request(
    base_url: str,
    session_id: str,
    reference_content: str,
    model: str = "gpt-5-mini",
) -> dict:
    messages = build_messages(reference_content)
    payload = {
        "model": model,
        "max_tokens": 512,
        "messages": messages,
        "stream": False,
    }

    print(f"\n→ POST {base_url}/v1/messages")
    print(f"  session : {session_id}")
    print(f"  messages: {len(messages)}개")
    print(f"  reference: {len(reference_content):,} chars / {reference_content.count(chr(10))+1:,} lines")

    with httpx.Client(timeout=120) as client:
        resp = client.post(
            f"{base_url}/v1/messages",
            json=payload,
            headers={
                "content-type": "application/json",
                "x-api-key": "dummy",
                "anthropic-version": "2023-06-01",
                "x-ccim-session": session_id,
            },
        )

    if resp.status_code != 200:
        print(f"\n[오류] HTTP {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)

    return resp.json()


# ─────────────────────────────────────────────────────────────────
# DB 쿼리 (선택)
# ─────────────────────────────────────────────────────────────────

async def query_db(session_id: str, db_url: str) -> dict | None:
    """PostgreSQL에서 해당 세션의 텔레메트리 조회."""
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(db_url, pool_pre_ping=True)
        async with engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT session_id,
                           tokens_input_original,
                           tokens_input_compressed,
                           tokens_output,
                           latency_ms,
                           retrieve_original_calls,
                           feature_flags
                    FROM requests
                    WHERE session_id = :sid
                    ORDER BY created_at DESC
                    LIMIT 1
                """),
                {"sid": session_id},
            )
            row = result.mappings().fetchone()
        await engine.dispose()
        return dict(row) if row else None
    except Exception as exc:
        print(f"[DB 쿼리 실패] {exc}")
        return None


# ─────────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────────

def print_result(resp: dict, db_row: dict | None) -> None:
    usage = resp.get("usage") or {}
    print("\n" + "=" * 56)
    print("  CCIM 압축 직접 검증 결과")
    print("=" * 56)

    # API 응답 기반
    input_t = usage.get("input_tokens", "?")
    output_t = usage.get("output_tokens", "?")
    print("\n  [API 응답 usage]")
    print(f"  입력 토큰  : {input_t}")
    print(f"  출력 토큰  : {output_t}")

    # DB 기반 (정확한 압축 전/후 비교)
    if db_row:
        orig = db_row.get("tokens_input_original") or 0
        comp = db_row.get("tokens_input_compressed") or 0
        saved = orig - comp
        pct = saved / orig * 100 if orig else 0
        retrieve = db_row.get("retrieve_original_calls", 0)

        print("\n  [DB 텔레메트리]")
        print(f"  입력 (원본)  : {orig:,} t")
        print(f"  입력 (압축후): {comp:,} t")
        print(f"  절감        : {saved:,} t  ({pct:.1f}%)")
        print(f"  지연        : {db_row.get('latency_ms', '?')} ms")
        print(f"  retrieve 호출: {retrieve}")

        if saved > 0:
            goal = "✓ 압축 발동!" if pct >= 10 else f"△ 압축 발동 ({pct:.1f}%)"
            print(f"\n  {goal}")
        else:
            print("\n  ✗ 압축 미발동 — CCIM 로그를 확인하세요")
            print("    예상 로그: '[compress] N candidate message(s) selected'")
            print_compression_diagnostics(db_row)
    else:
        print("\n  (DB 조회 생략 또는 실패 — --no-db 옵션 또는 DB 미연결)")

    print("=" * 56 + "\n")


def print_compression_diagnostics(db_row: dict) -> None:
    """DB feature_flags에 기록된 압축 skip 원인을 출력."""
    flags = db_row.get("feature_flags") or {}
    if not flags:
        return

    reason = flags.get("compress_skip_reason")
    total = flags.get("compress_total_tokens")
    threshold = flags.get("compress_threshold_tokens")
    target = flags.get("compress_target_tokens")
    eligible = flags.get("compress_eligible_messages")
    compressible = flags.get("compress_compressible_messages")
    selected = flags.get("compress_selected_messages")

    print("\n  [압축 진단]")
    if reason:
        print(f"  skip reason : {reason}")
    if total is not None and threshold is not None:
        print(f"  trigger     : total={total:,} / threshold={threshold:,}")
    if target is not None:
        print(f"  target      : {target:,}")
    if eligible is not None and compressible is not None and selected is not None:
        print(f"  candidates  : eligible={eligible}, compressible={compressible}, selected={selected}")

    if reason == "below_threshold":
        print("  조치        : CCIM_COMPRESSION_TRIGGER_TOKENS를 total 이하로 낮추거나 더 큰 reference를 사용하세요.")
    elif reason == "target_already_met":
        print("  조치        : CCIM_COMPRESSION_TARGET_TOKENS를 total보다 낮게 설정하세요.")


# ─────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="CCIM ToolResultBlock 압축 직접 검증")
    parser.add_argument("--url", default="http://localhost:8081", help="CCIM 게이트웨이 URL")
    parser.add_argument("--session", default="", help="세션 ID (기본: direct-<timestamp>)")
    parser.add_argument(
        "--reference",
        default="tools/compare/reference_pipeline.py",
        help="압축 대상 reference_pipeline.py 경로",
    )
    parser.add_argument("--model", default=default_model(), help="LLM 모델명")
    parser.add_argument("--no-db", action="store_true", help="DB 쿼리 생략")
    args = parser.parse_args()

    # reference_pipeline.py 읽기
    reference_path = Path(args.reference)
    if not reference_path.exists():
        print(f"[오류] 파일을 찾을 수 없음: {reference_path}")
        sys.exit(1)
    reference_content = reference_path.read_text(encoding="utf-8")

    session_id = args.session or f"direct-{int(time.time())}"

    # 요청 전송
    resp = send_request(args.url, session_id, reference_content, args.model)

    # DB 조회 (잠시 대기 후)
    db_row: dict | None = None
    if not args.no_db:
        time.sleep(1)  # 텔레메트리 INSERT 완료 대기
        db_url = os.environ.get(
            "CCIM_DATABASE_URL",
            "postgresql+psycopg://ccim:ccim@localhost:5432/ccim",
        )
        import sys as _sys
        if _sys.platform == "win32":
            import asyncio
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        db_row = asyncio.run(query_db(session_id, db_url))

    print_result(resp, db_row)


if __name__ == "__main__":
    main()

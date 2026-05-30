"""Generate a large Python reference file for compression-rate checks."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "tools" / "compare" / "large_reference.py"
FUNCTION_COUNT = 40


HEADER = '''"""Large reference module for CCIM compression-rate checks.

This file is generated and intentionally repetitive. It is not application
code. The goal is to provide enough Python function bodies for AST compression
to dominate the request context during manual Claude Code tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass
class LargeRecord:
    record_id: str
    tenant: str
    score: float
    tags: tuple[str, ...]
    payload: Mapping[str, Any]


def _coerce_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    return float(text or "0")


'''


FUNC_TEMPLATE = '''def transform_batch_{idx:03d}(records: Iterable[Mapping[str, Any]]) -> list[LargeRecord]:
    output: list[LargeRecord] = []
    seen: set[str] = set()
    for offset, raw in enumerate(records):
        record_id = str(raw.get("record_id") or raw.get("id") or f"batch-{idx:03d}-{{offset}}")
        if record_id in seen:
            continue
        seen.add(record_id)

        tenant = str(raw.get("tenant") or "default").strip().lower()
        score = _coerce_number(raw.get("score", 0)) + {bonus}
        tags: list[str] = []
        for tag in raw.get("tags", ()):
            text = str(tag).strip().lower()
            if text and text not in tags:
                tags.append(text)

        payload = dict(raw.get("payload") or {{}})
        payload["batch"] = {idx}
        payload["offset"] = offset
        payload["score_band"] = "high" if score >= 80 else "normal"

        if score < 0:
            payload["status"] = "rejected"
        elif "review" in tags or score > 95:
            payload["status"] = "review"
        else:
            payload["status"] = "accepted"

        output.append(
            LargeRecord(
                record_id=record_id,
                tenant=tenant,
                score=score,
                tags=tuple(tags),
                payload=payload,
            )
        )
    return output


'''


FOOTER = '''def run_all(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    total = 0
    accepted = 0
    review = 0
    rejected = 0
    for item in transform_batch_001(records):
        total += 1
        status = item.payload.get("status")
        if status == "accepted":
            accepted += 1
        elif status == "review":
            review += 1
        elif status == "rejected":
            rejected += 1
    return {
        "total": total,
        "accepted": accepted,
        "review": review,
        "rejected": rejected,
    }
'''


def main() -> None:
    parts = [HEADER]
    for idx in range(1, FUNCTION_COUNT + 1):
        parts.append(FUNC_TEMPLATE.format(idx=idx, bonus=idx % 7))
    parts.append(FOOTER)
    OUTPUT.write_text("".join(parts), encoding="utf-8")
    print(f"WROTE {OUTPUT} lines={OUTPUT.read_text(encoding='utf-8').count(chr(10)) + 1}")


if __name__ == "__main__":
    main()

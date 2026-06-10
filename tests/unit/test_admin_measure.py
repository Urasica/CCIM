from __future__ import annotations

from tools.admin_ui import measure


def test_summarize_measure_requests_includes_operational_costs() -> None:
    rows = [
        {
            "tokens_input_original": 1000,
            "tokens_input_compressed": 600,
            "tokens_output": 120,
            "latency_ms": 200,
            "retrieve_original_calls": 2,
            "feature_flags": {
                "compress_context_ids": 2,
                "retrieve_original_result_tokens_est": 40,
                "retrieve_original_tool_use_tokens_est": 10,
                "retrieve_original_cache_hits": 1,
                "retrieve_original_store_fetches": 1,
                "evidence_reload_hit": 1,
                "evidence_persistent_store_hit": 1,
                "evidence_redis_warm_loads": 1,
                "current_turn_write_guard_blocked": True,
            },
        },
        {
            "tokens_input_original": 800,
            "tokens_input_compressed": 500,
            "tokens_output": 80,
            "latency_ms": 300,
            "retrieve_original_calls": 1,
            "feature_flags": {
                "compress_context_ids": 0,
                "retrieve_original_result_tokens_est": 30,
                "retrieve_original_tool_use_tokens_est": 5,
                "retrieve_original_cache_hits": 2,
                "retrieve_original_store_fetches": 0,
                "evidence_reload_miss": 1,
                "evidence_persistent_store_miss": 1,
                "evidence_guard_blocked": True,
                "evidence_guard_version_mismatches": 1,
            },
        },
    ]

    summary = measure.summarize_measure_requests(rows)

    assert summary["requests"] == 2
    assert summary["saved_input_tokens"] == 700
    assert summary["saved_input_pct"] == 38.9
    assert summary["net_saved_input_tokens_est"] == 615
    assert summary["retrieve_original_calls"] == 3
    assert summary["retrieve_result_tokens_est"] == 70
    assert summary["retrieve_tool_use_tokens_est"] == 15
    assert summary["retrieve_cache_hits"] == 3
    assert summary["retrieve_store_fetches"] == 1
    assert summary["evidence_reload_hits"] == 1
    assert summary["evidence_reload_misses"] == 1
    assert summary["evidence_persistent_store_hits"] == 1
    assert summary["evidence_persistent_store_misses"] == 1
    assert summary["evidence_redis_warm_loads"] == 1
    assert summary["guard_blocks"] == 2
    assert summary["current_turn_guard_blocks"] == 1
    assert summary["evidence_guard_blocks"] == 1
    assert summary["evidence_guard_version_mismatches"] == 1
    assert summary["compressed_requests"] == 1
    assert summary["avg_latency_ms"] == 250


def test_render_markdown_report_escapes_labels_and_compacts_flags() -> None:
    data = {
        "since": 30,
        "left": {
            "label": "base|run",
            "summary": {
                "requests": 1,
                "total_input_original": 1000,
                "total_input_compressed": 1000,
                "total_output": 200,
                "total_tokens_sent": 1200,
                "saved_input_tokens": 0,
                "net_saved_input_tokens_est": 0,
                "retrieve_original_calls": 0,
                "retrieve_result_tokens_est": 0,
                "retrieve_cache_hits": 0,
                "retrieve_store_fetches": 0,
                "evidence_reload_hits": 0,
                "evidence_reload_misses": 0,
                "evidence_persistent_store_hits": 0,
                "evidence_persistent_store_misses": 0,
                "evidence_redis_warm_loads": 0,
                "guard_blocks": 0,
                "evidence_guard_blocks": 0,
                "evidence_guard_version_mismatches": 0,
                "avg_latency_ms": 120,
            },
            "requests": [
                {
                    "created_at": "2026-06-05T01:02:03",
                    "tokens_input_original": 1000,
                    "tokens_input_compressed": 1000,
                    "tokens_output": 200,
                    "latency_ms": 120,
                    "retrieve_original_calls": 0,
                    "feature_flags": {},
                }
            ],
        },
        "right": {
            "label": "ccim",
            "summary": {
                "requests": 1,
                "total_input_original": 1000,
                "total_input_compressed": 640,
                "total_output": 210,
                "total_tokens_sent": 850,
                "saved_input_tokens": 360,
                "net_saved_input_tokens_est": 300,
                "retrieve_original_calls": 1,
                "retrieve_result_tokens_est": 50,
                "retrieve_cache_hits": 1,
                "retrieve_store_fetches": 0,
                "evidence_reload_hits": 1,
                "evidence_reload_misses": 0,
                "evidence_persistent_store_hits": 1,
                "evidence_persistent_store_misses": 0,
                "evidence_redis_warm_loads": 1,
                "guard_blocks": 1,
                "evidence_guard_blocks": 1,
                "evidence_guard_version_mismatches": 1,
                "avg_latency_ms": 150,
            },
            "requests": [
                {
                    "created_at": "2026-06-05T01:03:04",
                    "tokens_input_original": 1000,
                    "tokens_input_compressed": 640,
                    "tokens_output": 210,
                    "latency_ms": 150,
                    "retrieve_original_calls": 1,
                    "feature_flags": {
                        "compress_context_ids": 2,
                        "retrieve_original_result_tokens_est": 50,
                        "evidence_reload_hit": 1,
                        "current_turn_write_guard_blocked": True,
                        "current_turn_write_guard_block_reason": "blocked_no_retrieve",
                        "evidence_guard_blocked": True,
                        "evidence_guard_block_reason": "blocked_version_mismatch",
                        "evidence_guard_version_mismatches": 1,
                        "stream_response_mode": "synthesized_complete_sse",
                    },
                }
            ],
        },
    }

    report = measure.render_markdown_report(data)

    assert report.startswith("# CCIM Benchmark Report")
    assert "| Net saved est. | 0 | 300 | +300 |" in report
    assert "| base\\|run | 1 | 2026-06-05T01:02" in report
    assert (
        "ctx=2 ret_t=50 reload=1/0 guard=blocked_no_retrieve "
        "evidence_guard=blocked_version_mismatch version_mismatch=1 "
        "stream=synthesized_complete_sse"
    ) in report

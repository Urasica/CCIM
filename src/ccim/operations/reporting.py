"""Deterministic, category-separated operational summaries."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ccim.operations.contracts import (
    SCHEMA_VERSION,
    ObservationStatus,
    ReportLabel,
    RequestObservation,
    RunMetadata,
)
from ccim.operations.safety import assert_artifact_safe

_TERMINAL_STATUSES = {
    ObservationStatus.SUCCEEDED,
    ObservationStatus.FAILED,
    ObservationStatus.SKIPPED,
}


def _run_summary(
    run: RunMetadata, observations: list[RequestObservation]
) -> dict[str, Any]:
    unique_ids = {item.logical_request_id for item in observations}
    complete_ids = {
        item.logical_request_id
        for item in observations
        if item.telemetry_complete and item.status in _TERMINAL_STATUSES
    }
    metric_samples = [
        item
        for item in observations
        if item.telemetry_complete
        and item.tokens_input_original_est is not None
        and item.tokens_input_sent_est is not None
    ]
    gross_saved = (
        sum(
            max(
                (item.tokens_input_original_est or 0)
                - (item.tokens_input_sent_est or 0),
                0,
            )
            for item in metric_samples
        )
        if metric_samples
        else None
    )
    retrieve_overhead = (
        sum(item.retrieve_overhead_tokens_est or 0 for item in metric_samples)
        if metric_samples
        else None
    )
    net_saved = (
        gross_saved - retrieve_overhead
        if gross_saved is not None and retrieve_overhead is not None
        else None
    )
    planned = run.planned_requests
    if len(unique_ids) > planned:
        raise ValueError("observed logical requests exceed planned_requests")
    completeness = round(len(complete_ids) / planned * 100, 2) if planned else None
    status_counts = Counter(item.status.value for item in observations)
    return {
        "run": run.as_dict(),
        "attempt_records": len(observations),
        "observed_requests": len(unique_ids),
        "telemetry_complete_requests": len(complete_ids),
        "missing_or_incomplete_requests": max(planned - len(complete_ids), 0),
        "telemetry_completeness_pct": completeness,
        "metric_sample_count": len(metric_samples),
        "gross_saved_tokens_est": gross_saved,
        "retrieve_overhead_tokens_est": retrieve_overhead,
        "net_saved_tokens_est": net_saved,
        "provider_usage_sample_count": sum(
            item.provider_input_tokens is not None for item in observations
        ),
        "status_counts": dict(sorted(status_counts.items())),
    }


def build_report(
    runs: list[RunMetadata],
    observations: list[RequestObservation],
    *,
    window_days: int,
    report_label: ReportLabel,
) -> dict[str, Any]:
    if window_days not in {7, 30}:
        raise ValueError("roadmap 02 supports deterministic 7-day or 30-day templates")
    if report_label is ReportLabel.ACTUAL:
        raise ValueError("roadmap 02 cannot generate actual-data reports")
    run_by_id = {run.run_id: run for run in runs}
    if len(run_by_id) != len(runs):
        raise ValueError("duplicate run_id")
    unknown = sorted({item.run_id for item in observations} - set(run_by_id))
    if unknown:
        raise ValueError(f"observations reference unknown run: {unknown[0]}")
    observation_keys = {
        (item.run_id, item.logical_request_id, item.attempt)
        for item in observations
    }
    if len(observation_keys) != len(observations):
        raise ValueError("duplicate request observation attempt")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in sorted(runs, key=lambda item: (item.run_category.value, item.run_id)):
        summary = _run_summary(
            run,
            [item for item in observations if item.run_id == run.run_id],
        )
        grouped[run.run_category.value].append(summary)

    categories = []
    for category in sorted(grouped):
        summaries = grouped[category]
        categories.append(
            {
                "run_category": category,
                "run_count": len(summaries),
                "planned_requests": sum(
                    item["run"]["planned_requests"] for item in summaries
                ),
                "telemetry_complete_requests": sum(
                    item["telemetry_complete_requests"] for item in summaries
                ),
                "runs": summaries,
            }
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "report_label": report_label.value,
        "actual_data": False,
        "window_days": window_days,
        "category_separation": "strict",
        "categories": categories,
    }
    assert_artifact_safe(report)
    return report

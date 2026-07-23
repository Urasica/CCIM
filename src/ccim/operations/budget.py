"""Deterministic preflight for the GPT-5 mini shared-token canary budget."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ccim.operations.contracts import ProjectMode, RunCategory


@dataclass(frozen=True)
class BudgetPolicy:
    daily_shared_limit: int = 2_500_000
    daily_hard_stop: int = 2_100_000
    request_input_cap: int = 180_000
    request_output_cap: int = 20_000
    request_envelope_cap: int = 200_000
    run_hard_cap: int = 900_000

    def __post_init__(self) -> None:
        values = asdict(self)
        if any(value <= 0 for value in values.values()):
            raise ValueError("budget limits must be positive")
        if self.daily_hard_stop >= self.daily_shared_limit:
            raise ValueError("daily hard stop must preserve a safety reserve")
        if (
            self.request_input_cap + self.request_output_cap
            > self.request_envelope_cap
        ):
            raise ValueError("request component caps must fit the envelope cap")
        if self.request_envelope_cap > self.run_hard_cap:
            raise ValueError("request envelope must fit the run cap")

    @property
    def safety_reserve(self) -> int:
        return self.daily_shared_limit - self.daily_hard_stop


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason_code: str
    request_envelope: int
    daily_tokens_after_request: int
    run_tokens_after_request: int
    remaining_to_hard_stop: int
    remaining_run_budget: int
    safety_reserve: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_preflight(
    *,
    run_category: RunCategory,
    project_mode: ProjectMode,
    known_daily_tokens: int,
    current_run_tokens: int,
    expected_input_tokens: int,
    max_output_tokens: int,
    usage_certain: bool,
    policy: BudgetPolicy | None = None,
) -> BudgetDecision:
    """Return a stable allow/deny decision without making a provider call."""
    active = policy or BudgetPolicy()
    numeric = (
        known_daily_tokens,
        current_run_tokens,
        expected_input_tokens,
        max_output_tokens,
    )
    envelope = max(expected_input_tokens, 0) + max(max_output_tokens, 0)

    def decision(allowed: bool, reason: str) -> BudgetDecision:
        daily_after = known_daily_tokens + envelope
        run_after = current_run_tokens + envelope
        return BudgetDecision(
            allowed=allowed,
            reason_code=reason,
            request_envelope=envelope,
            daily_tokens_after_request=daily_after,
            run_tokens_after_request=run_after,
            remaining_to_hard_stop=max(active.daily_hard_stop - daily_after, 0),
            remaining_run_budget=max(active.run_hard_cap - run_after, 0),
            safety_reserve=active.safety_reserve,
        )

    if any(value < 0 for value in numeric):
        return decision(False, "invalid_negative_value")
    if run_category is RunCategory.SYNTHETIC_DRY_RUN:
        return decision(False, "dry_run_external_call_forbidden")
    if run_category is not RunCategory.DAILY_CANARY:
        return decision(False, "category_not_shared_canary")
    if project_mode is not ProjectMode.SHARED_SYNTHETIC:
        return decision(False, "shared_synthetic_project_required")
    if not usage_certain:
        return decision(False, "usage_uncertain")
    if expected_input_tokens > active.request_input_cap:
        return decision(False, "request_input_cap")
    if max_output_tokens > active.request_output_cap:
        return decision(False, "request_output_cap")
    if envelope > active.request_envelope_cap:
        return decision(False, "request_envelope_cap")
    if current_run_tokens + envelope > active.run_hard_cap:
        return decision(False, "run_hard_cap")
    if known_daily_tokens + envelope > active.daily_hard_stop:
        return decision(False, "daily_hard_stop")
    return decision(True, "allowed")

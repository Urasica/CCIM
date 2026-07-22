"""Reference workflow pipeline for CCIM compression comparison tasks.

This module is intentionally larger than a tiny unit-test fixture. It gives
coding agents enough Python code to read so CCIM can exercise ToolResultBlock
compression, while keeping the actual task small and isolated.

The code models a deterministic event-processing pipeline:

* records are normalized into a stable shape;
* rules decide whether records should be accepted, flagged, or rejected;
* aggregators produce summary counters for downstream reporting;
* a small runner wires these pieces together for tests and examples.

The comparison task in tests/compare/task.md copies this file into a workspace
and modifies only the copy. Do not use this file as application code.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from statistics import mean
from typing import Any


class Severity(StrEnum):
    """Severity attached to a decision."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Action(StrEnum):
    """Action returned by rule evaluation."""

    ACCEPT = "accept"
    FLAG = "flag"
    REJECT = "reject"


@dataclass(frozen=True)
class Event:
    """A normalized input record."""

    event_id: str
    kind: str
    tenant: str
    user_id: str
    value: float
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def get(self, key: str, default: Any = None) -> Any:
        """Return a field or metadata value by dotted key."""
        if hasattr(self, key):
            return getattr(self, key)
        if "." not in key:
            return self.metadata.get(key, default)
        current: Any = self.metadata
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current


@dataclass(frozen=True)
class Decision:
    """Result of applying one or more rules to an event."""

    event_id: str
    action: Action
    severity: Severity
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.action == Action.ACCEPT

    def with_reason(self, reason: str) -> Decision:
        return Decision(
            event_id=self.event_id,
            action=self.action,
            severity=self.severity,
            reasons=(*self.reasons, reason),
        )


@dataclass(frozen=True)
class Rule:
    """A single condition with an action to emit when it matches."""

    name: str
    field: str
    operator: str
    expected: Any
    action: Action = Action.FLAG
    severity: Severity = Severity.WARNING


class Normalizer:
    """Convert raw dictionaries into Event objects."""

    def normalize(self, raw: Mapping[str, Any]) -> Event:
        event_id = str(raw.get("event_id") or raw.get("id") or "")
        if not event_id:
            raise ValueError("event_id is required")
        kind = str(raw.get("kind") or "generic").strip().lower()
        tenant = str(raw.get("tenant") or "default").strip().lower()
        user_id = str(raw.get("user_id") or raw.get("user") or "anonymous")
        value = self._coerce_float(raw.get("value", 0.0))
        tags = self._coerce_tags(raw.get("tags", ()))
        metadata = self._coerce_metadata(raw.get("metadata", {}))
        created_at = self._coerce_datetime(raw.get("created_at"))
        return Event(
            event_id=event_id,
            kind=kind,
            tenant=tenant,
            user_id=user_id,
            value=value,
            tags=tags,
            metadata=metadata,
            created_at=created_at,
        )

    def _coerce_float(self, value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if not text:
            return 0.0
        return float(text.replace(",", ""))

    def _coerce_tags(self, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        parts = value.split(",") if isinstance(value, str) else list(value)
        cleaned = []
        for item in parts:
            tag = str(item).strip().lower()
            if tag and tag not in cleaned:
                cleaned.append(tag)
        return tuple(cleaned)

    def _coerce_metadata(self, value: Any) -> Mapping[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        return {"raw_metadata": value}

    def _coerce_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str) and value:
            text = value.replace("Z", "+00:00")
            return datetime.fromisoformat(text)
        return datetime.now(UTC)


class ConditionEvaluator:
    """Evaluate simple rule conditions against events."""

    def evaluate(self, event: Event, rule: Rule) -> bool:
        actual = event.get(rule.field)
        return self._compare(actual, rule.operator, rule.expected)

    def _compare(self, actual: Any, operator: str, expected: Any) -> bool:
        op = operator.strip().lower()
        if op in {"eq", "=="}:
            return actual == expected
        if op in {"ne", "!="}:
            return actual != expected
        if op in {"gt", ">"}:
            return self._as_number(actual) > self._as_number(expected)
        if op in {"ge", ">="}:
            return self._as_number(actual) >= self._as_number(expected)
        if op in {"lt", "<"}:
            return self._as_number(actual) < self._as_number(expected)
        if op in {"le", "<="}:
            return self._as_number(actual) <= self._as_number(expected)
        if op == "in":
            return actual in expected
        if op == "not_in":
            return actual not in expected
        if op == "has_tag":
            return str(expected).lower() in {str(tag).lower() for tag in actual or ()}
        if op == "prefix":
            return str(actual).startswith(str(expected))
        if op == "suffix":
            return str(actual).endswith(str(expected))
        raise ValueError(f"unsupported operator: {operator}")

    def _as_number(self, value: Any) -> float:
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(value)
        return float(str(value).strip())


class RuleEngine:
    """Apply rules in order and return the most severe decision."""

    def __init__(self, rules: Sequence[Rule]) -> None:
        self.rules = tuple(rules)
        self.evaluator = ConditionEvaluator()

    def decide(self, event: Event) -> Decision:
        decision = Decision(event.event_id, Action.ACCEPT, Severity.INFO)
        for rule in self.rules:
            if not self.evaluator.evaluate(event, rule):
                continue
            decision = self._merge(decision, rule)
            if decision.action == Action.REJECT:
                break
        return decision

    def _merge(self, current: Decision, rule: Rule) -> Decision:
        action = self._max_action(current.action, rule.action)
        severity = self._max_severity(current.severity, rule.severity)
        return Decision(
            event_id=current.event_id,
            action=action,
            severity=severity,
            reasons=(*current.reasons, rule.name),
        )

    def _max_action(self, left: Action, right: Action) -> Action:
        order = {Action.ACCEPT: 0, Action.FLAG: 1, Action.REJECT: 2}
        return left if order[left] >= order[right] else right

    def _max_severity(self, left: Severity, right: Severity) -> Severity:
        order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}
        return left if order[left] >= order[right] else right


@dataclass
class Summary:
    """Aggregate output for a pipeline run."""

    total: int = 0
    accepted: int = 0
    flagged: int = 0
    rejected: int = 0
    total_value: float = 0.0
    max_value: float = 0.0
    min_value: float = 0.0
    reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "accepted": self.accepted,
            "flagged": self.flagged,
            "rejected": self.rejected,
            "total_value": round(self.total_value, 2),
            "max_value": round(self.max_value, 2),
            "min_value": round(self.min_value, 2),
            "reasons": dict(sorted(self.reasons.items())),
        }


class SummaryBuilder:
    """Build summary counters from events and decisions."""

    def build(self, rows: Iterable[tuple[Event, Decision]]) -> Summary:
        summary = Summary()
        values: list[float] = []
        for event, decision in rows:
            summary.total += 1
            summary.total_value += event.value
            values.append(event.value)
            if decision.action == Action.ACCEPT:
                summary.accepted += 1
            elif decision.action == Action.FLAG:
                summary.flagged += 1
            elif decision.action == Action.REJECT:
                summary.rejected += 1
            for reason in decision.reasons:
                summary.reasons[reason] = summary.reasons.get(reason, 0) + 1
        if values:
            summary.max_value = max(values)
            summary.min_value = min(values)
        return summary


class WindowedStats:
    """Keep simple per-tenant rolling statistics."""

    def __init__(self) -> None:
        self._values: dict[str, list[float]] = {}

    def add(self, event: Event) -> None:
        values = self._values.setdefault(event.tenant, [])
        values.append(event.value)
        if len(values) > 20:
            del values[:-20]

    def average(self, tenant: str) -> float:
        values = self._values.get(tenant, [])
        return mean(values) if values else 0.0

    def snapshot(self) -> dict[str, float]:
        return {tenant: round(mean(values), 2) for tenant, values in self._values.items()}


class EventStore:
    """In-memory event storage used by the reference pipeline."""

    def __init__(self) -> None:
        self._events: dict[str, Event] = {}
        self._decisions: dict[str, Decision] = {}

    def save(self, event: Event, decision: Decision) -> None:
        self._events[event.event_id] = event
        self._decisions[event.event_id] = decision

    def get_event(self, event_id: str) -> Event | None:
        return self._events.get(event_id)

    def get_decision(self, event_id: str) -> Decision | None:
        return self._decisions.get(event_id)

    def iter_rows(self) -> Iterator[tuple[Event, Decision]]:
        for event_id in sorted(self._events):
            event = self._events[event_id]
            decision = self._decisions[event_id]
            yield event, decision

    def clear(self) -> None:
        self._events.clear()
        self._decisions.clear()


class Pipeline:
    """Normalize, decide, store, and summarize raw records."""

    def __init__(self, rules: Sequence[Rule]) -> None:
        self.normalizer = Normalizer()
        self.engine = RuleEngine(rules)
        self.summary_builder = SummaryBuilder()
        self.stats = WindowedStats()
        self.store = EventStore()

    def process(self, raw_records: Iterable[Mapping[str, Any]]) -> Summary:
        for raw in raw_records:
            event = self.normalizer.normalize(raw)
            decision = self.engine.decide(event)
            self.stats.add(event)
            self.store.save(event, decision)
        return self.summary_builder.build(self.store.iter_rows())

    def explain(self, event_id: str) -> dict[str, Any]:
        event = self.store.get_event(event_id)
        decision = self.store.get_decision(event_id)
        if event is None or decision is None:
            raise KeyError(event_id)
        return {
            "event_id": event.event_id,
            "tenant": event.tenant,
            "kind": event.kind,
            "action": decision.action.value,
            "severity": decision.severity.value,
            "reasons": list(decision.reasons),
            "tenant_average": round(self.stats.average(event.tenant), 2),
        }


def default_rules() -> tuple[Rule, ...]:
    """Return a baseline rule set for examples and tests."""
    return (
        Rule(
            name="negative-value",
            field="value",
            operator="<",
            expected=0,
            action=Action.REJECT,
            severity=Severity.ERROR,
        ),
        Rule(
            name="large-payment",
            field="value",
            operator=">",
            expected=1000,
            action=Action.FLAG,
            severity=Severity.WARNING,
        ),
        Rule(
            name="manual-review-tag",
            field="tags",
            operator="has_tag",
            expected="review",
            action=Action.FLAG,
            severity=Severity.WARNING,
        ),
        Rule(
            name="blocked-kind",
            field="kind",
            operator="in",
            expected={"blocked", "malformed"},
            action=Action.REJECT,
            severity=Severity.ERROR,
        ),
    )


def sample_records() -> list[dict[str, Any]]:
    """Return sample records that exercise the default rules."""
    return [
        {
            "event_id": "evt-001",
            "kind": "payment",
            "tenant": "alpha",
            "user_id": "u-1",
            "value": "1250.50",
            "tags": ["vip", "review"],
            "metadata": {"region": "apac", "device": {"type": "mobile"}},
        },
        {
            "event_id": "evt-002",
            "kind": "refund",
            "tenant": "alpha",
            "user_id": "u-2",
            "value": 42,
            "tags": "routine",
            "metadata": {"region": "emea", "device": {"type": "desktop"}},
        },
        {
            "event_id": "evt-003",
            "kind": "blocked",
            "tenant": "beta",
            "user_id": "u-3",
            "value": 10,
            "tags": [],
            "metadata": {"region": "apac", "device": {"type": "kiosk"}},
        },
        {
            "event_id": "evt-004",
            "kind": "payment",
            "tenant": "beta",
            "user_id": "u-4",
            "value": -2,
            "tags": ["review"],
            "metadata": {"region": "na", "device": {"type": "mobile"}},
        },
    ]


def run_demo() -> dict[str, Any]:
    """Run the pipeline and return a deterministic report."""
    pipeline = Pipeline(default_rules())
    summary = pipeline.process(sample_records())
    explanations = [
        pipeline.explain("evt-001"),
        pipeline.explain("evt-002"),
        pipeline.explain("evt-003"),
        pipeline.explain("evt-004"),
    ]
    return {
        "summary": summary.as_dict(),
        "tenant_stats": pipeline.stats.snapshot(),
        "explanations": explanations,
    }


def _format_report(report: Mapping[str, Any]) -> str:
    """Format a report for command-line display."""
    lines = ["Reference pipeline report", "=" * 25]
    summary = report["summary"]
    for key in ("total", "accepted", "flagged", "rejected", "total_value"):
        lines.append(f"{key}: {summary[key]}")
    lines.append("tenant_stats:")
    for tenant, value in sorted(report["tenant_stats"].items()):
        lines.append(f"  - {tenant}: {value}")
    lines.append("explanations:")
    for item in report["explanations"]:
        lines.append(
            "  - {event_id}: {action}/{severity} reasons={reasons}".format(**item)
        )
    return "\n".join(lines)


if __name__ == "__main__":
    print(_format_report(run_demo()))

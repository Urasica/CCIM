"""Evidence action guard for agent workflows built on CCIM.

The guard does not decide domain meaning. It only checks that an agent action
which depends on compressed evidence has retrieved the required original spans,
and optionally verifies that the expected document versions still match.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ccim.reversibility.store import (
    DEFAULT_DOCUMENT_VERSION,
    ContextRecord,
    ReversibilityStore,
)


@dataclass(frozen=True)
class EvidenceGuardRequest:
    """Guard input supplied by an agent before a final/evidence-backed action."""

    action_type: str
    required_context_ids: Sequence[str] = ()
    expected_session_id: str | None = None
    expected_document_versions: Mapping[str, int] = field(default_factory=dict)
    expected_context_versions: Mapping[str, int] = field(default_factory=dict)
    allow_empty_evidence: bool = False

    def __post_init__(self) -> None:
        if not self.action_type:
            raise ValueError("action_type must not be empty")


@dataclass(frozen=True)
class EvidenceVersionMismatch:
    """Version mismatch found while validating an evidence action."""

    context_id: str
    document_id: str | None
    expected_version: int
    actual_version: int


@dataclass(frozen=True)
class EvidenceGuardDecision:
    """Guard result that can be surfaced in telemetry or to an agent."""

    allowed: bool
    action_type: str
    reason: str | None
    required_context_ids: list[str]
    retrieved_context_ids: list[str]
    missing_context_ids: list[str] = field(default_factory=list)
    validated_context_ids: list[str] = field(default_factory=list)
    version_mismatches: list[EvidenceVersionMismatch] = field(default_factory=list)
    records_checked: int = 0

    def to_feature_flags(self, prefix: str = "evidence_guard") -> dict[str, Any]:
        return {
            f"{prefix}_blocked": not self.allowed,
            f"{prefix}_action_type": self.action_type,
            f"{prefix}_block_reason": self.reason if not self.allowed else None,
            f"{prefix}_required_contexts": len(self.required_context_ids),
            f"{prefix}_retrieved_contexts": len(self.retrieved_context_ids),
            f"{prefix}_missing_contexts": len(self.missing_context_ids),
            f"{prefix}_validated_contexts": len(self.validated_context_ids),
            f"{prefix}_records_checked": self.records_checked,
            f"{prefix}_version_mismatches": len(self.version_mismatches),
            f"{prefix}_validated_context_ids": list(self.validated_context_ids),
        }


class EvidenceGuard:
    """Validate retrieve-before-final-action rules for evidence workflows."""

    def __init__(self, store: ReversibilityStore | None = None) -> None:
        self._store = store

    async def evaluate(
        self,
        request: EvidenceGuardRequest,
        *,
        retrieved_contexts: Mapping[str, str] | Iterable[str],
    ) -> EvidenceGuardDecision:
        required = _unique_context_ids(request.required_context_ids)
        retrieved = _context_id_set(retrieved_contexts)

        if not required and not request.allow_empty_evidence:
            return _decision(
                request,
                required,
                retrieved,
                allowed=False,
                reason="missing_required_contexts",
            )

        cross_session = _cross_session_contexts(required, request.expected_session_id)
        if cross_session:
            return _decision(
                request,
                required,
                retrieved,
                allowed=False,
                reason="blocked_cross_session_context",
                missing=cross_session,
            )

        missing = [context_id for context_id in required if context_id not in retrieved]
        if missing:
            reason = "blocked_no_retrieve" if len(missing) == len(required) else "blocked_incomplete_retrieve"
            return _decision(
                request,
                required,
                retrieved,
                allowed=False,
                reason=reason,
                missing=missing,
            )

        records_checked = 0
        mismatches: list[EvidenceVersionMismatch] = []
        if request.expected_document_versions or request.expected_context_versions:
            if self._store is None:
                return _decision(
                    request,
                    required,
                    retrieved,
                    allowed=False,
                    reason="blocked_version_check_unavailable",
                )
            for full_context_id in required:
                record = await self._get_record(full_context_id)
                if record is None:
                    return _decision(
                        request,
                        required,
                        retrieved,
                        allowed=False,
                        reason="blocked_context_record_missing",
                    )
                records_checked += 1
                mismatch = _version_mismatch(full_context_id, record, request)
                if mismatch is not None:
                    mismatches.append(mismatch)

        if mismatches:
            return _decision(
                request,
                required,
                retrieved,
                allowed=False,
                reason="blocked_version_mismatch",
                mismatches=mismatches,
                records_checked=records_checked,
            )

        return _decision(
            request,
            required,
            retrieved,
            allowed=True,
            reason="allowed_after_retrieve",
            validated=required,
            records_checked=records_checked,
        )

    async def _get_record(self, full_context_id: str) -> ContextRecord | None:
        if self._store is None or ":" not in full_context_id:
            return None
        session_id, _, context_id = full_context_id.partition(":")
        return await self._store.get(session_id, context_id)


def _decision(
    request: EvidenceGuardRequest,
    required: list[str],
    retrieved: set[str],
    *,
    allowed: bool,
    reason: str | None,
    missing: list[str] | None = None,
    validated: list[str] | None = None,
    mismatches: list[EvidenceVersionMismatch] | None = None,
    records_checked: int = 0,
) -> EvidenceGuardDecision:
    return EvidenceGuardDecision(
        allowed=allowed,
        action_type=request.action_type,
        reason=reason,
        required_context_ids=required,
        retrieved_context_ids=sorted(retrieved),
        missing_context_ids=missing or [],
        validated_context_ids=validated or [],
        version_mismatches=mismatches or [],
        records_checked=records_checked,
    )


def _unique_context_ids(context_ids: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(item for item in context_ids if isinstance(item, str) and item))


def _context_id_set(contexts: Mapping[str, str] | Iterable[str]) -> set[str]:
    if isinstance(contexts, Mapping):
        return {key for key, value in contexts.items() if isinstance(key, str) and value}
    return {item for item in contexts if isinstance(item, str) and item}


def _cross_session_contexts(context_ids: list[str], expected_session_id: str | None) -> list[str]:
    if expected_session_id is None:
        return []
    return [
        context_id
        for context_id in context_ids
        if ":" not in context_id or context_id.partition(":")[0] != expected_session_id
    ]


def _version_mismatch(
    full_context_id: str,
    record: ContextRecord,
    request: EvidenceGuardRequest,
) -> EvidenceVersionMismatch | None:
    context_id = full_context_id.partition(":")[2]
    actual_version = record.document_version or DEFAULT_DOCUMENT_VERSION
    expected_version = _expected_version(full_context_id, context_id, record, request)
    if expected_version is None or expected_version == actual_version:
        return None
    return EvidenceVersionMismatch(
        context_id=full_context_id,
        document_id=record.document_id,
        expected_version=expected_version,
        actual_version=actual_version,
    )


def _expected_version(
    full_context_id: str,
    context_id: str,
    record: ContextRecord,
    request: EvidenceGuardRequest,
) -> int | None:
    if full_context_id in request.expected_context_versions:
        return request.expected_context_versions[full_context_id]
    if context_id in request.expected_context_versions:
        return request.expected_context_versions[context_id]
    if record.document_id and record.document_id in request.expected_document_versions:
        return request.expected_document_versions[record.document_id]
    return None

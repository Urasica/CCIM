"""S(system) > D(developer) > U(user) > R(retrieved/RAG) 4-compartment data structures.

PCFI core thesis: a higher compartment's instructions cannot be overridden by a lower one.
Any role-switch attempt from below is treated as injection.

V1 classification rules:
  - S: `system` field + role=system messages
  - D: `tools` definitions (treated as developer instructions)
  - U: regular user/assistant messages
  - R: messages explicitly tagged by caller via `rag_messages`

Anthropic Messages API has no 'developer' role, so D is populated from tool defs.
RAG auto-detection (e.g. metadata.source="retrieval") is deferred to V2.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from ccim.api.schemas import ContentBlock, Message


class Section(IntEnum):
    """4-compartment. Integer order = privilege (lower wins)."""

    S = 0
    D = 1
    U = 2
    R = 3


@dataclass(frozen=True)
class Compartment:
    section: Section
    messages: list[Message] = field(default_factory=list)


@dataclass(frozen=True)
class Compartments:
    """Decomposed request. Order: S -> D -> U -> R."""

    s: Compartment
    d: Compartment
    u: Compartment
    r: Compartment

    def iter_low_priority(self) -> Iterable[Compartment]:
        """U and R only; PCFI scans these for injection patterns."""
        return (self.u, self.r)

    def iter_scannable_messages(self) -> Iterable[tuple[Section, Message]]:
        """Return only end-user-controlled content for PCFI scanning.

        Assistant history remains in U for conversation continuity, but is excluded
        from injection scanning to avoid blocking on prior examples/quotes.
        """
        for msg in self.u.messages:
            if msg.role == "user":
                yield Section.U, msg
        for msg in self.r.messages:
            yield Section.R, msg

    @classmethod
    def from_request(
        cls,
        system: str | list[ContentBlock] | None = None,
        messages: Iterable[Message] | None = None,
        tools: Iterable[Any] | None = None,
        rag_messages: Iterable[Message] | None = None,
    ) -> "Compartments":
        s_msgs: list[Message] = []
        d_msgs: list[Message] = []
        u_msgs: list[Message] = []
        r_msgs: list[Message] = list(rag_messages) if rag_messages else []

        if system is not None:
            s_msgs.append(Message(role="system", content=system))

        if tools:
            tool_payload: list[Any] = []
            for t in tools:
                if hasattr(t, "model_dump"):
                    tool_payload.append(t.model_dump())
                elif isinstance(t, dict):
                    tool_payload.append(t)
                else:
                    tool_payload.append({"raw": str(t)})
            d_msgs.append(
                Message(
                    role="system",
                    content="[tool_definitions]\n"
                    + json.dumps(tool_payload, ensure_ascii=False),
                )
            )

        for m in messages or []:
            if m.role == "system":
                s_msgs.append(m)
            else:
                u_msgs.append(m)

        return cls(
            s=Compartment(section=Section.S, messages=s_msgs),
            d=Compartment(section=Section.D, messages=d_msgs),
            u=Compartment(section=Section.U, messages=u_msgs),
            r=Compartment(section=Section.R, messages=r_msgs),
        )

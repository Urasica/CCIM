"""압축 마커 헬퍼.

형식: `<<CTX_{session}:{ctx_id}>>` — LLM 프롬프트에 들어가는 placeholder.
LLM이 이 마커를 보고 `retrieve_original(context_id=...)`를 호출하도록 유도.
"""

from __future__ import annotations

import re
from typing import NamedTuple

_MARKER_RE = re.compile(r"<<CTX_(?P<session>[A-Za-z0-9\-]+):(?P<ctx>[A-Za-z0-9\-_]+)>>")


class MarkerRef(NamedTuple):
    session_id: str
    context_id: str

    @property
    def full_id(self) -> str:
        return f"{self.session_id}:{self.context_id}"


def build_marker(session_id: str, context_id: str) -> str:
    return f"<<CTX_{session_id}:{context_id}>>"


def parse_marker(text: str) -> MarkerRef | None:
    m = _MARKER_RE.fullmatch(text.strip())
    if not m:
        return None
    return MarkerRef(session_id=m["session"], context_id=m["ctx"])


def find_all_markers(text: str) -> list[MarkerRef]:
    return [MarkerRef(m["session"], m["ctx"]) for m in _MARKER_RE.finditer(text)]

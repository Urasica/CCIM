"""Request payload models for the admin API."""

from __future__ import annotations

from pydantic import BaseModel


class SettingsPayload(BaseModel):
    values: dict[str, str]


class MeasurePayload(BaseModel):
    left: str
    right: str
    since: int = 120
    verbose: bool = True

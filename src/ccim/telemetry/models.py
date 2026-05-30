"""SQLAlchemy 모델 (PostgreSQL `requests` 테이블)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RequestRow(Base):
    """`requests` 테이블 매핑. 스키마 정의는 migrations/001_init.sql."""

    __tablename__ = "requests"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String, nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String, nullable=True)

    pcfi_action: Mapped[str] = mapped_column(String, nullable=False)
    pcfi_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    tokens_input_original: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_input_compressed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_output: Mapped[int | None] = mapped_column(Integer, nullable=True)

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pcfi_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compress_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    retrieve_original_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    write_remaps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    feature_flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    version: Mapped[str] = mapped_column(String, default="v1.0", nullable=False)

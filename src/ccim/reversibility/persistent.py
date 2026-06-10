"""Persistent evidence store implementations.

The persistent store is a cold backing store for Redis. Redis remains the hot
runtime cache; this module keeps enough context payloads to reload Redis after
restart or TTL loss.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from ccim.reversibility.store import (
    ContextRecord,
    _context_record_from_payload,
    _context_record_to_payload,
)


class SQLiteEvidenceStore:
    """SQLite-backed persistent evidence store.

    This uses stdlib sqlite3 and opens a short-lived connection per operation so
    the async wrapper can safely run blocking work in a thread.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    async def put_context(self, record: ContextRecord) -> None:
        await asyncio.to_thread(self._put_context_sync, record)

    async def get_context(self, session_id: str, context_id: str) -> ContextRecord | None:
        return await asyncio.to_thread(self._get_context_sync, session_id, context_id)

    async def get_contexts_by_document_hash(self, document_hash: str) -> list[ContextRecord]:
        return await asyncio.to_thread(self._get_contexts_by_document_hash_sync, document_hash)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_contexts (
                    session_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    document_id TEXT,
                    document_hash TEXT,
                    document_version INTEGER,
                    source_kind TEXT,
                    span_type TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, context_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_evidence_contexts_document_hash
                ON evidence_contexts(document_hash)
                """
            )

    def _put_context_sync(self, record: ContextRecord) -> None:
        payload = _context_record_to_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO evidence_contexts (
                    session_id,
                    context_id,
                    document_id,
                    document_hash,
                    document_version,
                    source_kind,
                    span_type,
                    payload,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.session_id,
                    record.context_id,
                    record.document_id,
                    record.document_hash,
                    record.document_version,
                    record.source_kind,
                    record.span_type,
                    json.dumps(payload, ensure_ascii=False),
                    record.created_at.isoformat(),
                ),
            )

    def _get_context_sync(self, session_id: str, context_id: str) -> ContextRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload
                FROM evidence_contexts
                WHERE session_id = ? AND context_id = ?
                """,
                (session_id, context_id),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(session_id, context_id, row)

    def _get_contexts_by_document_hash_sync(self, document_hash: str) -> list[ContextRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, context_id, payload
                FROM evidence_contexts
                WHERE document_hash = ?
                ORDER BY created_at ASC, context_id ASC
                """,
                (document_hash,),
            ).fetchall()
        return [
            _record_from_row(str(row["session_id"]), str(row["context_id"]), row)
            for row in rows
        ]


def _record_from_row(session_id: str, context_id: str, row: sqlite3.Row) -> ContextRecord:
    payload: dict[str, Any] = json.loads(str(row["payload"]))
    return _context_record_from_payload(session_id, context_id, payload)

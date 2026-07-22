from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from ccim.migrations import (
    apply_migrations,
    check_database,
    discover_migrations,
    normalize_database_url,
)

pytestmark = pytest.mark.integration


def _url_for_database(base_url: str, database: str) -> str:
    parsed = urlsplit(normalize_database_url(base_url))
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", "", ""))


@contextmanager
def _temporary_database(base_url: str) -> Iterator[str]:
    name = f"ccim_migration_{uuid.uuid4().hex[:12]}"
    admin_url = _url_for_database(base_url, "postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield _url_for_database(base_url, name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (name,),
            )
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def _test_database_url() -> str:
    value = os.getenv("CCIM_TEST_DATABASE_URL")
    if not value:
        pytest.skip("CCIM_TEST_DATABASE_URL is not configured")
    return value


def test_migration_apply_is_idempotent_on_new_database() -> None:
    base_url = _test_database_url()
    migrations = discover_migrations()
    with _temporary_database(base_url) as database_url:
        first = apply_migrations(database_url, migrations)
        second = apply_migrations(database_url, migrations)
        checked = check_database(database_url, migrations)

    assert first.current is True
    assert second.current is True
    assert checked.current is True
    assert checked.applied_versions == checked.expected_versions


def test_migration_apply_adopts_existing_idempotent_schema() -> None:
    base_url = _test_database_url()
    migrations = discover_migrations()
    with _temporary_database(base_url) as database_url:
        with psycopg.connect(normalize_database_url(database_url)) as connection:
            for migration in migrations:
                connection.execute(migration.sql)

        before = check_database(database_url, migrations)
        after = apply_migrations(database_url, migrations)

    assert before.current is False
    assert before.missing_versions == tuple(item.version for item in migrations)
    assert after.current is True

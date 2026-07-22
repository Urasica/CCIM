from __future__ import annotations

from pathlib import Path

import pytest

from ccim.migrations import (
    MigrationError,
    compare_migration_state,
    discover_migrations,
    normalize_database_url,
)


def test_discover_migrations_orders_and_hashes_files(tmp_path: Path) -> None:
    (tmp_path / "002_second.sql").write_text("SELECT 2;\n", encoding="utf-8")
    (tmp_path / "001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("ignored", encoding="utf-8")

    migrations = discover_migrations(tmp_path)

    assert [item.version for item in migrations] == [1, 2]
    assert [item.name for item in migrations] == ["first", "second"]
    assert all(len(item.checksum) == 64 for item in migrations)


def test_discover_migrations_rejects_duplicate_version(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_second.sql").write_text("SELECT 2;", encoding="utf-8")

    with pytest.raises(MigrationError, match="duplicate migration version"):
        discover_migrations(tmp_path)


def test_compare_migration_state_reports_each_inconsistency(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "002_second.sql").write_text("SELECT 2;", encoding="utf-8")
    migrations = discover_migrations(tmp_path)

    state = compare_migration_state(
        migrations,
        [(1, "wrong-checksum"), (3, "unexpected")],
    )

    assert state.current is False
    assert state.missing_versions == (2,)
    assert state.unexpected_versions == (3,)
    assert state.checksum_mismatches == (1,)


def test_compare_migration_state_accepts_exact_ledger(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    migrations = discover_migrations(tmp_path)

    state = compare_migration_state(
        migrations,
        [(migrations[0].version, migrations[0].checksum)],
    )

    assert state.current is True
    assert state.status == "current"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql+psycopg://user:pass@db/name", "postgresql://user:pass@db/name"),
        ("postgresql+asyncpg://user:pass@db/name", "postgresql://user:pass@db/name"),
        ("postgresql://user:pass@db/name", "postgresql://user:pass@db/name"),
    ],
)
def test_normalize_database_url(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected

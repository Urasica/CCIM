"""Versioned PostgreSQL migration inspection and application CLI.

Usage:
    python -m ccim.migrations check
    python -m ccim.migrations apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ccim.config import get_settings

_MIGRATION_NAME_RE = re.compile(r"^(?P<version>[0-9]+)_(?P<name>.+)\.sql$")
_LEDGER_TABLE = "ccim_schema_migrations"


class MigrationError(RuntimeError):
    """Raised when migration files or the database ledger are inconsistent."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str
    path: Path


@dataclass(frozen=True)
class MigrationState:
    status: str
    current: bool
    expected_versions: tuple[int, ...]
    applied_versions: tuple[int, ...]
    missing_versions: tuple[int, ...]
    unexpected_versions: tuple[int, ...]
    checksum_mismatches: tuple[int, ...]

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def default_migrations_dir() -> Path:
    """Find the repository/image migration directory without using user paths."""
    candidates = (
        Path.cwd() / "migrations",
        Path(__file__).resolve().parents[2] / "migrations",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise MigrationError("migrations directory not found")


def discover_migrations(root: Path | None = None) -> tuple[Migration, ...]:
    directory = root or default_migrations_dir()
    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME_RE.fullmatch(path.name)
        if match is None:
            continue
        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version: {version}")
        seen_versions.add(version)
        raw = path.read_bytes()
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                checksum=hashlib.sha256(raw).hexdigest(),
                sql=raw.decode("utf-8"),
                path=path,
            )
        )
    if not migrations:
        raise MigrationError(f"no versioned migrations found in {directory}")
    return tuple(sorted(migrations, key=lambda item: item.version))


def normalize_database_url(database_url: str) -> str:
    """Convert SQLAlchemy driver URLs to a psycopg connection URL."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


def compare_migration_state(
    migrations: Sequence[Migration], applied_rows: Sequence[tuple[int, str]]
) -> MigrationState:
    expected = {item.version: item.checksum for item in migrations}
    applied = {int(version): str(checksum) for version, checksum in applied_rows}
    missing = tuple(sorted(set(expected) - set(applied)))
    unexpected = tuple(sorted(set(applied) - set(expected)))
    mismatches = tuple(
        sorted(
            version
            for version in set(expected) & set(applied)
            if expected[version] != applied[version]
        )
    )
    current = not missing and not unexpected and not mismatches
    return MigrationState(
        status="current" if current else "outdated",
        current=current,
        expected_versions=tuple(sorted(expected)),
        applied_versions=tuple(sorted(applied)),
        missing_versions=missing,
        unexpected_versions=unexpected,
        checksum_mismatches=mismatches,
    )


def _ensure_ledger(connection: Any) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_LEDGER_TABLE} (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def inspect_database(connection: Any, migrations: Sequence[Migration]) -> MigrationState:
    ledger_exists = connection.execute(
        "SELECT to_regclass(%s)", (f"public.{_LEDGER_TABLE}",)
    ).fetchone()[0]
    if ledger_exists is None:
        return compare_migration_state(migrations, ())
    rows = connection.execute(
        f"SELECT version, checksum FROM {_LEDGER_TABLE} ORDER BY version"
    ).fetchall()
    return compare_migration_state(migrations, rows)


def check_database(database_url: str, migrations: Sequence[Migration]) -> MigrationState:
    import psycopg

    with psycopg.connect(normalize_database_url(database_url)) as connection:
        return inspect_database(connection, migrations)


async def inspect_async_engine(
    engine: Any, migrations: Sequence[Migration]
) -> MigrationState:
    """Inspect the migration ledger through the application's async engine."""
    from sqlalchemy import text

    async with engine.connect() as connection:
        ledger_exists = (
            await connection.execute(
                text("SELECT to_regclass(:ledger)"),
                {"ledger": f"public.{_LEDGER_TABLE}"},
            )
        ).scalar()
        if ledger_exists is None:
            return compare_migration_state(migrations, ())
        rows = (
            await connection.execute(
                text(
                    f"SELECT version, checksum FROM {_LEDGER_TABLE} ORDER BY version"
                )
            )
        ).all()
    return compare_migration_state(migrations, rows)


def apply_migrations(database_url: str, migrations: Sequence[Migration]) -> MigrationState:
    import psycopg

    with psycopg.connect(normalize_database_url(database_url)) as connection:
        with connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('ccim-schema-migrations'))"
            )
            _ensure_ledger(connection)
            rows = connection.execute(
                f"SELECT version, checksum FROM {_LEDGER_TABLE} ORDER BY version"
            ).fetchall()
            applied = {int(version): str(checksum) for version, checksum in rows}
            for migration in migrations:
                previous = applied.get(migration.version)
                if previous is not None:
                    if previous != migration.checksum:
                        raise MigrationError(
                            f"migration {migration.version} checksum changed after apply"
                        )
                    continue
                connection.execute(migration.sql)
                connection.execute(
                    f"""
                    INSERT INTO {_LEDGER_TABLE} (version, name, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.name, migration.checksum),
                )
        return inspect_database(connection, migrations)


def _print_state(state: MigrationState, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(state.as_json(), sort_keys=True))
        return
    print(
        "migration_status="
        f"{state.status} applied={list(state.applied_versions)} "
        f"expected={list(state.expected_versions)} missing={list(state.missing_versions)} "
        f"unexpected={list(state.unexpected_versions)} "
        f"checksum_mismatches={list(state.checksum_mismatches)}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect or apply CCIM migrations")
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--migrations-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = args.database_url or get_settings().database_url
    try:
        migrations = discover_migrations(args.migrations_dir)
        if args.command == "apply":
            state = apply_migrations(database_url, migrations)
        else:
            state = check_database(database_url, migrations)
    except Exception as exc:
        print(f"migration_status=error error={type(exc).__name__}: {exc}")
        return 1
    _print_state(state, as_json=args.json)
    return 0 if state.current else 1


if __name__ == "__main__":
    raise SystemExit(main())

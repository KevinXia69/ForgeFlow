"""PostgreSQL control-store boundary. External calls never run in a transaction."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

DEFAULT_MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations" / "control"


class ControlStore:
    def __init__(self, database_url: str, migrations_dir: str | Path | None = None,
                 connection: psycopg.Connection | None = None) -> None:
        self.database_url = database_url
        self.migrations_dir = Path(migrations_dir) if migrations_dir else DEFAULT_MIGRATIONS
        self.connection = connection or psycopg.connect(
            database_url, autocommit=False, row_factory=dict_row, connect_timeout=5)

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor]:
        # Store calls are external boundaries. A caller may have left an
        # implicit read transaction open; commit it before opening the write
        # block, otherwise psycopg creates a savepoint and close rolls it back.
        if self.connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            self.connection.commit()
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                yield cursor

    def migrate(self) -> list[int]:
        applied: list[int] = []
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS control_schema_migrations "
            "(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {row["version"] for row in self.connection.execute(
            "SELECT version FROM control_schema_migrations").fetchall()}
        for path in sorted(self.migrations_dir.glob("*.sql")):
            version = int(path.stem.split("_", 1)[0])
            if version in done:
                continue
            with self.connection.transaction():
                self.connection.execute(path.read_text(encoding="utf-8"))
                self.connection.execute(
                    "INSERT INTO control_schema_migrations(version) VALUES (%s)", (version,))
            applied.append(version)
        # The migration table check above opens an implicit transaction. Commit
        # it here; otherwise a later close would roll back the DDL.
        self.connection.commit()
        return applied

    def seed_tenant(self, tenant_id: str, display_name: str, policy_version: int = 1,
                    principals: tuple[tuple[str, str, str], ...] = ()) -> None:
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO control_tenants(id, display_name, policy_version) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET display_name=EXCLUDED.display_name, "
                "policy_version=EXCLUDED.policy_version",
                (tenant_id, display_name, policy_version))
            for principal_id, name, role in principals:
                # (tenant_id, id) is the principal key; the same id in another
                # tenant is a different membership, never a silent rebind.
                self.connection.execute(
                    "INSERT INTO control_principals(id, tenant_id, display_name, role) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, id) DO UPDATE SET "
                    "display_name=EXCLUDED.display_name, role=EXCLUDED.role",
                    (principal_id, tenant_id, name, role))

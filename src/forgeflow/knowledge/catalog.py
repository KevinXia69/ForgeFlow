"""Trusted knowledge publication catalog backed by the control PostgreSQL store.

Publishing is serialised per tenant.  Two concurrent publishers cannot both
retire the other's generation, and a caller that prepared against an older
sequence gets a conflict instead of silently overwriting newer knowledge.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from ..superagent.store import ControlStore


class PublicationConflict(RuntimeError):
    """A publish was built against knowledge that has since been superseded."""

    def __init__(self, code: str = "publication_stale") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PublishedGeneration:
    tenant_id: str
    generation: str
    state: str
    sequence: int = 0


class KnowledgeCatalog:
    def __init__(self, store: ControlStore) -> None:
        self.store = store

    @classmethod
    def from_env(cls, database_url: str) -> "KnowledgeCatalog":
        return cls(ControlStore(database_url))

    # -- generations ---------------------------------------------------------
    def new_generation(self, tenant_id: str) -> str:
        generation = str(uuid4())
        with self.store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO knowledge_generations(tenant_id, generation, state) "
                "VALUES (%s, %s, 'staging')", (tenant_id, generation))
        return generation

    def publish(self, tenant_id: str, generation: str, *, expected_sequence: int | None = None,
                actor: str | None = None, document_count: int | None = None,
                point_count: int | None = None) -> PublishedGeneration:
        UUID(generation)
        with self.store.transaction() as cursor:
            # The counter row is both the per-tenant lock and the monotonic
            # publish sequence.
            cursor.execute(
                "INSERT INTO knowledge_publication_counters(tenant_id, last_sequence) "
                "VALUES (%s, 0) ON CONFLICT (tenant_id) DO NOTHING", (tenant_id,))
            last = int(cursor.execute(
                "SELECT last_sequence FROM knowledge_publication_counters "
                "WHERE tenant_id=%s FOR UPDATE", (tenant_id,)).fetchone()["last_sequence"])
            current = cursor.execute(
                "SELECT generation, publish_sequence FROM knowledge_generations "
                "WHERE tenant_id=%s AND state='published'", (tenant_id,)).fetchone()
            current_sequence = int(current["publish_sequence"] or 0) if current else 0
            row = cursor.execute(
                "SELECT state FROM knowledge_generations WHERE tenant_id=%s AND generation=%s",
                (tenant_id, generation)).fetchone()
            if row is None:
                raise LookupError("generation_not_found")
            if row["state"] == "published":
                return PublishedGeneration(tenant_id, generation, "published",
                                           current_sequence)
            if expected_sequence is not None and current_sequence > int(expected_sequence):
                raise PublicationConflict("publication_stale")
            sequence = max(last, current_sequence) + 1
            cursor.execute(
                "UPDATE knowledge_publication_counters SET last_sequence=%s WHERE tenant_id=%s",
                (sequence, tenant_id))
            cursor.execute(
                "UPDATE knowledge_generations SET state='retired' "
                "WHERE tenant_id=%s AND state='published'", (tenant_id,))
            cursor.execute(
                "UPDATE knowledge_generations SET state='published', published_at=now(), "
                "publish_sequence=%s, published_by=%s, document_count=%s, point_count=%s "
                "WHERE tenant_id=%s AND generation=%s",
                (sequence, actor, document_count, point_count, tenant_id, generation))
        return PublishedGeneration(tenant_id, generation, "published", sequence)

    def retire(self, tenant_id: str, generation: str) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE knowledge_generations SET state='retired' "
                "WHERE tenant_id=%s AND generation=%s AND state <> 'retired'",
                (tenant_id, generation))

    def published(self, tenant_id: str) -> str | None:
        row = self.store.connection.execute(
            "SELECT generation FROM knowledge_generations "
            "WHERE tenant_id=%s AND state='published'", (tenant_id,)).fetchone()
        self.store.connection.commit()
        return str(row["generation"]) if row else None

    def published_sequence(self, tenant_id: str) -> int:
        row = self.store.connection.execute(
            "SELECT publish_sequence FROM knowledge_generations "
            "WHERE tenant_id=%s AND state='published'", (tenant_id,)).fetchone()
        self.store.connection.commit()
        return int(row["publish_sequence"] or 0) if row else 0

    def generation_state(self, tenant_id: str, generation: str) -> dict:
        row = self.store.connection.execute(
            "SELECT generation, state, publish_sequence, created_at, published_at, "
            "published_by, document_count, point_count FROM knowledge_generations "
            "WHERE tenant_id=%s AND generation=%s", (tenant_id, generation)).fetchone()
        self.store.connection.commit()
        if row is None:
            raise LookupError("generation_not_found")
        return {**dict(row), "generation": str(row["generation"])}

    def list_generations(self, tenant_id: str, limit: int = 50) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT generation, state, publish_sequence, created_at, published_at, "
            "published_by, document_count, point_count FROM knowledge_generations "
            "WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, max(1, min(int(limit), 200)))).fetchall()
        self.store.connection.commit()
        return [{**dict(row), "generation": str(row["generation"])} for row in rows]

    def close(self) -> None:
        self.store.close()

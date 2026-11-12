"""Service liveness heartbeats for supervisors and health endpoints."""
from __future__ import annotations

from psycopg.types.json import Jsonb

from .store import ControlStore


def beat(store: ControlStore, *, service_id: str, tenant_id: str | None, kind: str,
         detail: dict | None = None) -> None:
    if not service_id or not kind:
        raise ValueError("heartbeat_invalid")
    with store.transaction() as cursor:
        cursor.execute(
            "INSERT INTO control_service_heartbeats(service_id, tenant_id, kind, last_seen, detail) "
            "VALUES (%s, %s, %s, now(), %s) ON CONFLICT (service_id) DO UPDATE SET "
            "tenant_id=EXCLUDED.tenant_id, kind=EXCLUDED.kind, last_seen=now(), "
            "detail=EXCLUDED.detail",
            (service_id, tenant_id, kind, Jsonb(detail or {})))


def health(store: ControlStore, *, tenant_id: str | None = None) -> list[dict]:
    query = ("SELECT service_id, tenant_id, kind, last_seen, detail, "
             "(last_seen < now() - interval '90 seconds') AS stale "
             "FROM control_service_heartbeats")
    params = ()
    if tenant_id:
        query += " WHERE tenant_id=%s"
        params = (tenant_id,)
    query += " ORDER BY service_id"
    rows = store.connection.execute(query, params).fetchall()
    values = [dict(row) for row in rows]
    store.connection.commit()
    return values

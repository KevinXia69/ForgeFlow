"""Current, principal-authorized catalog snapshots for reviewed reference facts.

The release sidecar is never an authorization source. One database statement
binds every returned document and block to the current principal, persisted ACL
policy, and published generation; callers re-read this snapshot before replay.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReferenceSnapshot:
    fingerprint: str
    sources: dict[str, dict[str, Any]]


SNAPSHOT_SQL = """
SELECT d.*, b.tenant_id AS bound_tenant, COALESCE(%s, b.role) AS bound_role,
       g.generation AS current_generation, g.publish_sequence,
       COALESCE((SELECT jsonb_agg(to_jsonb(k) ORDER BY k.ord)
                 FROM knowledge_document_blocks k
                 WHERE k.document_id=d.id AND k.tenant_id=d.tenant_id
                   AND k.version=d.version), '[]'::jsonb) AS blocks
FROM control_odoo_bindings b
JOIN knowledge_role_scopes a ON a.tenant_id=b.tenant_id
  AND a.role=COALESCE(%s, b.role)
JOIN knowledge_documents d ON d.tenant_id=b.tenant_id AND d.acl_scope=a.acl_scope
JOIN knowledge_generations g ON g.tenant_id=d.tenant_id AND g.state='published'
  AND g.generation::text=d.published_generation AND g.generation::text=d.indexed_generation
WHERE b.principal_id=%s AND d.source_path=ANY(%s)
  AND (%s::text IS NULL OR b.tenant_id=%s)
  AND d.status='ready' AND d.withdrawn_at IS NULL
  AND d.parse_status='complete' AND d.review_state IN ('confirmed','not_required')
"""


def load_reference_snapshot(store, plan, principal_id: str,
                            *, account_role: str | None = None,
                            account_tenant: str | None = None) -> ReferenceSnapshot | None:
    """Return no snapshot unless all required current sources are authorized."""
    required = {item['source_path']: item for item in plan.sources}
    if not required or len(required) != len(plan.sources):
        return None
    rows = store.connection.execute(
        SNAPSHOT_SQL, (account_role, account_role, principal_id, list(required),
                       account_tenant, account_tenant)).fetchall()
    store.connection.commit()
    sources: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        path = row['source_path']
        if path not in required or path in sources:
            return None
        if row['sha256'] != required[path]['sha256'] or row.get('unparsed_scope'):
            return None
        if hashlib.sha256(row['content'].encode()).hexdigest() != row['content_hash']:
            return None
        blocks = row.pop('blocks')
        if not blocks:
            return None
        for block in blocks:
            if (int(block['version']) != int(row['version'])
                    or hashlib.sha256(block['text'].encode()).hexdigest() != block['content_hash']):
                return None
        sources[path] = {'catalog': row, 'blocks': blocks, 'version': int(row['version'])}
    if set(sources) != set(required):
        return None
    # Includes ids, versions, publication, role, lifecycle, parsed content and
    # block positions. An identical raw file republished as a new version is a
    # different answer binding. No content is placed in the stream journal key.
    payload = {'principal': principal_id, 'sidecar': plan.data_sha256, 'sources': sources}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str,
                                      ensure_ascii=False).encode()).hexdigest()
    return ReferenceSnapshot(digest, sources)

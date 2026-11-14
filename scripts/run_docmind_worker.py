#!/usr/bin/env python3
"""Run the Document Mind parse worker: submit, poll, write catalogue, index."""
from __future__ import annotations

import json
import os
import signal
import sys
import threading

from forgeflow.knowledge.docmind import DocMindConfig, DocumentMindClient
from forgeflow.knowledge.docmind_intake import write_result
from forgeflow.knowledge.docmind_jobs import ParseJobStore, ParseWorker
from forgeflow.superagent import ControlStore


def main() -> int:
    if not os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL"):
        print(json.dumps({"ok": False, "error": "control_database_required"}))
        return 2
    config = DocMindConfig.from_env()
    if config is None:
        print(json.dumps({"ok": False, "error": "docmind_credentials_missing",
                          "missing": DocMindConfig.missing()}))
        return 2
    store = ControlStore(os.environ["FORGEFLOW_CONTROL_DATABASE_URL"])
    store.migrate()
    jobs = ParseJobStore(store)
    client = DocumentMindClient(config)
    indexed = {"count": 0}

    def on_complete(job, result, blocks):
        written = write_result(store, job=job, result=result)
        try:
            from forgeflow.knowledge.catalog import KnowledgeCatalog
            from forgeflow.knowledge.documents_catalog import DocumentCatalog
            from forgeflow.semantic import SemanticConfig
            from forgeflow.semantic.index import index_catalog_documents, index_single_document

            config = SemanticConfig.from_env()
            catalog = KnowledgeCatalog(store)
            tenant_id = str(job["tenant_id"])
            # The first document must become searchable without an operator
            # pressing “publish”; later documents update the live generation.
            if catalog.published(tenant_id) is None:
                index_catalog_documents(config, store, tenant_id, catalog)
            else:
                index_single_document(
                    config, store, tenant_id, written["source_path"],
                    catalog, DocumentCatalog(store))
            written["indexed"] = True
            indexed["count"] += 1
        except Exception as error:  # indexing is a separate, retryable step
            written["indexed"] = False
            written["index_error"] = type(error).__name__
        return written

    worker = ParseWorker(jobs, client, on_complete=on_complete)
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    ticks = 0
    while not stop.is_set():
        try:
            worker.tick(limit=5)
        except Exception as error:  # keep the service alive; the job stays queued
            print(json.dumps({"worker_error": type(error).__name__}), flush=True)
        ticks += 1
        stop.wait(5)
    store.close()
    print(json.dumps({"ok": True, "ticks": ticks, "indexed": indexed["count"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Validate or install the versioned procurement reference pack for one tenant."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from forgeflow.knowledge.reference_pack import install_reference_pack, load_reference_pack


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1]
                        / "knowledge/reference/supply-recovery-v1")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Validate files without network or writes")
    action.add_argument("--tenant", help="Install into this existing server-bound tenant")
    args = parser.parse_args()
    store = None
    try:
        pack = load_reference_pack(args.root)
        if args.check:
            result = {"ok": True, "pack_id": pack.pack_id, "release": pack.release,
                      "documents": len(pack.documents), "mode": "check",
                      "source_paths": [document.source_path for document in pack.documents]}
        else:
            from forgeflow.semantic import SemanticConfig
            from forgeflow.superagent.store import ControlStore

            database_url = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "").strip()
            if not database_url:
                raise ValueError("control_database_required")
            config = SemanticConfig.from_env()
            store = ControlStore(database_url)
            store.migrate()
            result = install_reference_pack(pack, tenant_id=args.tenant,
                                            store=store, config=config)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        # Provider/database exceptions can contain credentials or request URLs.
        # Publish only our bounded error codes and the exception type.
        from forgeflow.knowledge.reference_pack import ReferencePackError

        code = str(error) if isinstance(error, ReferencePackError) else type(error).__name__
        if str(error) == "control_database_required":
            code = "control_database_required"
        print(json.dumps({"ok": False, "error": code}, ensure_ascii=False))
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())

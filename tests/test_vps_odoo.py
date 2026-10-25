"""Persistence boundary tests, independent of Docker and external services."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forgeflow import vps_odoo as runtime


class PersistentOdooTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binding = runtime.expected_binding("instance-A", "generation-1")

    def ready_state(self):
        state, fresh = runtime.load_state(self.root, self.binding, True)
        self.assertTrue(fresh)
        for name in ("postgresql", "odoo", "logs"):
            (self.root / name).mkdir()
        (self.root / "postgresql" / "PG_VERSION").write_bytes(b"18\n")
        artifacts = {"api_key": b"synthetic-test-key\n", "master_password": b"x" * 64,
                     "task-derivation.json": b'{"name":"planning-metadata-v1"}',
                     "planning-metadata-v1.diff": b"synthetic note-only diff",
                     "repair_seeded_records.json": json.dumps({
                         "purchase_orders": {"P": {"id": 1}},
                         "manufacturing_orders": {"M": {"id": 2}},
                     }).encode()}
        state["artifacts"] = {}
        for name, data in artifacts.items():
            (self.root / name).write_bytes(data)
            state["artifacts"][name] = runtime.digest(data)
        state["phase"] = "ready"
        runtime.save_manifest(self.root, state)
        return state

    def test_empty_volume_requires_explicit_initialization(self):
        with self.assertRaisesRegex(runtime.StateError, "explicit_initialization_required"):
            runtime.load_state(self.root, self.binding, False)
        (self.root / ".lock").touch()
        state, fresh = runtime.load_state(self.root, self.binding, True)
        self.assertTrue(fresh)
        self.assertEqual(state["phase"], "initializing")

    def test_existing_unrecognized_volume_is_never_reseeded(self):
        (self.root / "old-database").mkdir()
        with self.assertRaisesRegex(runtime.StateError, "unrecognized_existing_state"):
            runtime.load_state(self.root, self.binding, True)

    def test_interrupted_initialization_is_not_retried(self):
        state, _ = runtime.load_state(self.root, self.binding, True)
        runtime.save_manifest(self.root, state)
        with self.assertRaisesRegex(runtime.StateError, "incomplete_initialization"):
            runtime.load_state(self.root, self.binding, True)

    def test_ready_volume_resumes_even_when_allow_initialize_is_set(self):
        expected = self.ready_state()
        state, fresh = runtime.load_state(self.root, self.binding, True)
        self.assertFalse(fresh)
        self.assertEqual(state, expected)

    def test_volume_rejects_different_generation_and_task(self):
        self.ready_state()
        for key, value in (("generation", "generation-2"), ("instance_id", "B"),
                           ("task", "another-task"), ("upstream_commit", "another-commit"),
                           ("data_profile", "original")):
            with self.subTest(key=key):
                binding = {**self.binding, key: value}
                with self.assertRaisesRegex(runtime.StateError, "generation_binding_mismatch"):
                    runtime.load_state(self.root, binding, True)

    def test_missing_seed_is_not_recreated_from_database(self):
        self.ready_state()
        (self.root / "repair_seeded_records.json").unlink()
        with self.assertRaisesRegex(runtime.StateError, "state_artifact_missing"):
            runtime.load_state(self.root, self.binding, True)

    def test_tampered_key_or_seed_is_rejected(self):
        self.ready_state()
        for name in runtime.ARTIFACT_FILES:
            path = self.root / name
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.subTest(name=name):
                with self.assertRaisesRegex(runtime.StateError, "persistent_artifact_mismatch"):
                    runtime.load_state(self.root, self.binding, True)
            path.write_bytes(original)

    def test_wrong_postgresql_major_is_rejected(self):
        self.ready_state()
        (self.root / "postgresql" / "PG_VERSION").write_bytes(b"17\n")
        with self.assertRaisesRegex(runtime.StateError, "postgres_version_mismatch"):
            runtime.load_state(self.root, self.binding, True)

    def test_database_restore_must_match_volume_and_seed(self):
        state = self.ready_state()
        payload = runtime.database_binding(state)
        runtime.check_database_binding(state, json.dumps(payload))
        for key, value in (("nonce", "f" * 64), ("generation", "stale"),
                           ("artifacts", {"api_key": "different"})):
            with self.subTest(key=key):
                with self.assertRaisesRegex(runtime.StateError, "database_binding_mismatch"):
                    runtime.check_database_binding(state, json.dumps({**payload, key: value}))

    def test_seed_requires_original_receipt_shape(self):
        for data in (b"{}", b"null", b"[]", b"broken", b'{"purchase_orders":[]}'):
            with self.subTest(data=data):
                with self.assertRaisesRegex(runtime.StateError, "seed_receipt_invalid"):
                    runtime.validate_seed(data)

    def test_unknown_upstream_entrypoint_is_rejected(self):
        with self.assertRaisesRegex(runtime.StateError, "upstream_entrypoint_unrecognized"):
            runtime.preserving_source(b"#!/bin/sh\necho substituted\n")

    def test_guard_changes_are_reversible_and_do_not_publish_ready(self):
        # Synthetic shell source exercises boundaries; build separately enforces real SHA256.
        source = (b"#!/bin/bash\npg_ctlcluster 18 main start\n"
                  b"su postgres -s /bin/bash -c role\nexport HOST=127.0.0.1\n"
                  b"# Create database via HTTP POST\npython3 /setup/setup_scenario.py\n"
                  b"\nwhile true; do\ndone\n")
        with patch.object(runtime, "ENTRYPOINT_SHA256", runtime.digest(source)):
            output = runtime.preserving_source(source)
        opening = b"if [ ! -f /tmp/saas_setup_complete ]; then # ForgeFlow first boot\n"
        closing = b"fi # ForgeFlow first boot\n"
        self.assertEqual(output.count(opening), 2)
        self.assertNotIn(b"forgeflow_local_ready", output)
        unwrapped = output.replace(opening, b"").replace(closing, b"")
        unwrapped = unwrapped.replace(b"pg_ctlcluster 18 main status >/dev/null 2>&1 || ", b"")
        unwrapped = unwrapped.replace(b"python3 /opt/forgeflow/vps_odoo.py --run-original-setup",
                                      b"python3 /setup/setup_scenario.py")
        self.assertEqual(unwrapped, source)


if __name__ == "__main__":
    unittest.main()

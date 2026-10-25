"""The VPS image must use the existing planning visibility variant exactly."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


class OdooProjectionTests(unittest.TestCase):
    def test_original_scenario_is_retained_and_only_notes_are_derived(self):
        from forgeflow.runner import _planning_metadata_projection

        path = Path(__file__).resolve().parents[1] / "deploy/vps/odoo-project.py"
        spec = importlib.util.spec_from_file_location("vps_projection_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = {"workcenters": [{"code": "WC-A", "product_code": "P-A",
                    "time_per_unit_minutes": 2.5, "unit_cost": 4, "lead_days": 0,
                    "note": "Benchmark capacity: 300 total processing minutes across the scenario horizon."}],
                   "sales_orders": [{"deadline": 9, "quantity": 17}], "budget": 1500}
        original = (json.dumps(payload, indent=2) + "\n").encode()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination, evidence = root / "original.json", root / "execution.json", root / "evidence"
            source.write_bytes(original)
            record = module.derive(source, destination, evidence)
            expected, expected_record, expected_diff = _planning_metadata_projection(original.decode())
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(destination.read_bytes(), expected.encode())
            self.assertEqual((evidence / "planning-metadata-v1.diff").read_bytes(), expected_diff.encode())
            for key, value in expected_record.items():
                self.assertEqual(record[key], value)
            self.assertEqual(record["source_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(record["changed_fields"], ["/workcenters/0/note"])
            restored = json.loads(destination.read_bytes())
            restored["workcenters"][0]["note"] = payload["workcenters"][0]["note"]
            self.assertEqual(restored, payload)


if __name__ == "__main__":
    unittest.main()

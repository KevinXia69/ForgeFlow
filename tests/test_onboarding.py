from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from forgeflow.onboarding import OnboardingError, OnboardingService, mapping_targets


def _csv() -> str:
    text = "customer,sku,qty,due\nAlpine,SKU-1,50,2026-11-20\n"
    return base64.b64encode(text.encode()).decode()


def test_create_and_inspect_a_read_only_workspace(tmp_path: Path) -> None:
    service = OnboardingService(tmp_path / "onboarding")
    workspace = service.create("owner", "Acme Manufacturing", "Manufacturing", ["sale", "purchase"])
    assert workspace["status"] == "draft"
    assert workspace["version"] == 0
    assert workspace["warnings"] == ["read_only_context"]
    assert "owner" not in workspace
    assert service.list("owner")[0]["id"] == workspace["id"]
    assert workspace["states"] == {
        "saved": True, "mapping_confirmed": False, "context_adopted": False,
        "import_executed": False, "import_readback_passed": False,
    }


def test_legacy_flags_cannot_claim_workspace_knowledge_or_rule_publication(tmp_path: Path) -> None:
    root = tmp_path / "onboarding"
    service = OnboardingService(root)
    workspace = service.create("owner", "Acme", "Manufacturing", ["knowledge"])
    path = root / workspace["id"] / "workspace.json"
    manifest = json.loads(path.read_text())
    manifest.update(knowledge_published=True, rules_effective=True,
                    states={"knowledge_published": True, "rules_effective": True})
    path.write_text(json.dumps(manifest))
    for view in (service.get("owner", workspace["id"]), service.list("owner")[0]):
        assert "knowledge_published" not in view and "rules_effective" not in view
        assert view["states"] == workspace["states"]
    # Reading a legacy workspace preserves its stored manifest; only the API
    # projection changes, with no migration or invented lifecycle binding.
    assert json.loads(path.read_text())["knowledge_published"] is True


def test_csv_headers_produce_editable_odoo_mapping_proposals(tmp_path: Path) -> None:
    service = OnboardingService(tmp_path / "onboarding")
    workspace = service.create("owner", "Acme", "Manufacturing", ["sale"])
    updated = service.add_source("owner", workspace["id"], "orders.csv", "text/csv", _csv())
    assert updated["sources"][0]["headers"] == ["customer", "sku", "qty", "due"]
    assert updated["sources"][0]["row_count"] == 1
    mappings = {row["source_field"]: row for row in updated["mappings"]}
    assert mappings["customer"]["target_model"] == "res.partner"
    assert mappings["customer"]["target_field"] == "name"
    assert mappings["sku"]["target_model"] == "product.product"
    assert mappings["qty"]["status"] == "suggested"
    assert "orders.csv" not in str(updated.get("sources", [{}])[0].get("path", ""))


def test_confirmation_requires_every_mapping_and_records_a_version(tmp_path: Path) -> None:
    service = OnboardingService(tmp_path / "onboarding")
    workspace = service.create("owner", "Acme", "Manufacturing", ["sale"])
    updated = service.add_source("owner", workspace["id"], "orders.csv", "text/csv", _csv())
    mappings = updated["mappings"]
    with pytest.raises(OnboardingError) as incomplete:
        service.confirm("owner", workspace["id"], [{"id": mappings[0]["id"], "target_model": "res.partner", "target_field": "name"}])
    assert incomplete.value.code == "mappings_incomplete"
    confirmed = service.confirm("owner", workspace["id"], [
        {"id": row["id"], "target_model": row["target_model"], "target_field": row["target_field"]}
        if row["target_model"] else {"id": row["id"], "ignored": True}
        for row in mappings
    ])
    assert confirmed["status"] == "mapping_confirmed"
    assert confirmed["version"] == 1
    # Confirmation adopts the mapping as context; it must not claim an ERP import.
    assert confirmed["warnings"] == ["mapping_confirmed_not_imported"]
    assert confirmed["states"]["mapping_confirmed"] is True
    assert confirmed["states"]["context_adopted"] is True
    assert confirmed["states"]["import_executed"] is False
    assert confirmed["states"]["import_readback_passed"] is False
    adoption = confirmed["mapping_adoption"]
    assert adoption["status"] == "adopted"
    assert adoption["models"]
    assert sum(adoption["counts"].values()) == len(
        [row for row in mappings if row["target_model"]])
    assert len(adoption["mapping_sha256"]) == 64
    with pytest.raises(OnboardingError) as closed:
        service.add_source("owner", workspace["id"], "more.csv", "text/csv", _csv())
    assert closed.value.code == "workspace_confirmed"


def test_invalid_target_and_foreign_owner_are_rejected(tmp_path: Path) -> None:
    service = OnboardingService(tmp_path / "onboarding")
    workspace = service.create("owner", "Acme", "Manufacturing", [])
    updated = service.add_source("owner", workspace["id"], "orders.csv", "text/csv", _csv())
    mapping = updated["mappings"][0]
    with pytest.raises(OnboardingError) as bad:
        service.confirm("owner", workspace["id"], [{"id": mapping["id"], "target_model": "res.partner", "target_field": "not_a_field"}])
    assert bad.value.code == "invalid_mapping_target"
    with pytest.raises(OnboardingError) as foreign:
        service.get("other", workspace["id"])
    assert foreign.value.code == "workspace_not_found"
    assert service.list("other") == []


def test_target_registry_is_explicit_odoo_community_models() -> None:
    targets = mapping_targets()["models"]
    models = {row["model"] for row in targets}
    assert "res.partner" in models
    assert "sale.order" in models
    assert "mrp.production" in models
    assert all(row["fields"] for row in targets)


def test_pasted_material_requires_an_explicit_workspace(tmp_path: Path) -> None:
    service = OnboardingService(tmp_path / "onboarding")
    workspace = service.create("owner", "Acme", "Manufacturing", [])
    first = service.intake_text("owner", workspace["id"], "Supplier notes",
                                "Supplier A was late three times.")
    assert first["status"] == "draft"
    assert first["sources"][0]["filename"] == "Supplier-notes.md"
    second = service.intake_text("owner", workspace["id"], "More notes",
                                 "Backup supplier B is approved.")
    assert second["id"] == first["id"]
    assert len(second["sources"]) == 2
    # There is no implicit "newest draft": another owner cannot write here, and
    # an unknown workspace is not created on the fly.
    with pytest.raises(OnboardingError) as foreign:
        service.intake_text("other", workspace["id"], "x", "y")
    assert foreign.value.code == "workspace_not_found"
    assert service.list("other") == []
    with pytest.raises(OnboardingError) as missing:
        service.intake_text("owner", "0" * 32, "x", "y")
    assert missing.value.code == "workspace_not_found"


def test_pasted_material_after_confirmation_is_refused(tmp_path: Path) -> None:
    service = OnboardingService(tmp_path / "onboarding")
    workspace = service.create("owner", "Acme", "Manufacturing", [])
    updated = service.add_source("owner", workspace["id"], "orders.csv", "text/csv", _csv())
    service.confirm("owner", workspace["id"], [
        {"id": row["id"], "target_model": row["target_model"], "target_field": row["target_field"]}
        if row["target_model"] else {"id": row["id"], "ignored": True}
        for row in updated["mappings"]
    ])
    with pytest.raises(OnboardingError) as closed:
        service.intake_text("owner", workspace["id"], "New policy",
                            "Important customers come first.")
    assert closed.value.code == "workspace_confirmed"


def _csv_with_headers(count: int) -> str:
    header = ",".join(f"field_{index}" for index in range(count))
    return base64.b64encode((header + "\n" + ",".join("v" for _ in range(count)) + "\n").encode()).decode()


def test_field_limit_is_enforced_before_writing_a_source(tmp_path: Path) -> None:
    service = OnboardingService(tmp_path / "onboarding")
    workspace = service.create("owner", "Acme", "Manufacturing", [])
    accepted = service.add_source("owner", workspace["id"], "wide.csv", "text/csv", _csv_with_headers(500))
    assert len(accepted["sources"][0]["headers"]) == 500
    assert len(accepted["mappings"]) == 500
    with pytest.raises(OnboardingError) as too_many:
        service.add_source("owner", workspace["id"], "too-wide.csv", "text/csv", _csv_with_headers(501))
    assert too_many.value.code == "too_many_fields"
    assert len(service.get("owner", workspace["id"])["mappings"]) == 500
    confirmed = service.confirm("owner", workspace["id"], [
        {"id": row["id"], "ignored": True} for row in accepted["mappings"]
    ])
    assert confirmed["status"] == "mapping_confirmed"


def test_headers_are_not_truncated_for_large_but_valid_sources(tmp_path: Path) -> None:
    service = OnboardingService(tmp_path / "onboarding")
    workspace = service.create("owner", "Acme", "Manufacturing", [])
    updated = service.add_source("owner", workspace["id"], "wide.csv", "text/csv", _csv_with_headers(201))
    assert len(updated["sources"][0]["headers"]) == 201
    assert len(updated["mappings"]) == 201

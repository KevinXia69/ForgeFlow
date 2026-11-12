"""Mapping confirmation is not an ERP import; preview and idempotency are explicit."""
from __future__ import annotations

import pytest

from forgeflow.onboarding_import import (
    ImportError,
    authorize,
    build_preview,
    execute,
    readback,
)


class FakeAdapter:
    def __init__(self) -> None:
        self.records: dict[str, dict[int, dict]] = {"res.partner": {}, "product.product": {}}
        self.next_id = 100
        self.creates = 0
        self.writes = 0

    def add(self, model: str, **values):
        identifier = self.next_id
        self.next_id += 1
        self.records[model][identifier] = {"id": identifier, **values}
        return identifier

    def search_read(self, model, domain, fields, limit=100):
        rows = list(self.records[model].values())
        for field, operator, expected in domain:
            if operator == "=":
                rows = [row for row in rows if row.get(field) == expected]
        return [{name: row.get(name) for name in fields} | {"id": row["id"]}
                for row in rows[:limit]]

    def create(self, model, values):
        self.creates += 1
        return self.add(model, **values)

    def write(self, model, ids, values):
        self.writes += 1
        for identifier in ids:
            self.records[model][identifier].update(values)
        return True


def _workspace(**overrides):
    workspace = {
        "id": "a" * 32,
        "mapping_adoption": {"mapping_sha256": "b" * 64},
        "mappings": [
            {"id": "s1:0", "source_id": "s1", "source_field": "name",
             "target_model": "res.partner", "target_field": "name",
             "status": "confirmed"},
            {"id": "s1:1", "source_id": "s1", "source_field": "ref",
             "target_model": "res.partner", "target_field": "ref",
             "status": "confirmed"},
            {"id": "s1:2", "source_id": "s1", "source_field": "email",
             "target_model": "res.partner", "target_field": "email",
             "status": "confirmed"},
        ],
    }
    workspace.update(overrides)
    return workspace


def _records(rows):
    return {"s1": {"headers": ["name", "ref", "email"], "rows": rows}}


def test_preview_classifies_create_update_unchanged_and_duplicate() -> None:
    adapter = FakeAdapter()
    adapter.add("res.partner", name="Beta", ref="EXT-2", email="b@x")
    adapter.add("res.partner", name="Gamma", ref="EXT-3", email="old@x")
    preview = build_preview(_workspace(), _records([
        ["Alpha", "EXT-1", "a@x"],
        ["Beta", "EXT-2", "b@x"],
        ["Gamma", "EXT-3", "new@x"],
        ["Alpha again", "EXT-1", "a@x"],
    ]), adapter)
    actions = [row["action"] for row in preview["rows"]]
    assert actions == ["create", "unchanged", "update", "conflict"]
    assert preview["rows"][2]["changes"] == {"email": "new@x"}
    assert "duplicate_identity" in preview["rows"][3]["problems"]
    assert preview["counts"]["create"] == 1
    assert preview["importable"] is False
    assert adapter.creates == 0 and adapter.writes == 0


def test_a_source_that_spans_models_or_an_unwritable_field_is_blocked() -> None:
    mappings = [
        {"id": "s1:0", "source_id": "s1", "source_field": "name",
         "target_model": "res.partner", "target_field": "name", "status": "confirmed"},
        {"id": "s1:1", "source_id": "s1", "source_field": "ref",
         "target_model": "product.product", "target_field": "default_code",
         "status": "confirmed"},
    ]
    preview = build_preview(_workspace(mappings=mappings), _records(
        [["Alpha", "EXT-1", "a@x"]]), FakeAdapter())
    assert {"code": "source_spans_models", "source_id": "s1"} in preview["blocking"]
    assert preview["importable"] is False

    mappings[1].update(target_model="res.partner", target_field="country_id")
    preview = build_preview(_workspace(mappings=mappings), _records(
        [["Alpha", "EXT-1", "a@x"]]), FakeAdapter())
    assert any(item["code"] == "field_not_writable" for item in preview["blocking"])


def test_authorization_is_bound_to_the_exact_preview_digest() -> None:
    preview = build_preview(_workspace(), _records([["Alpha", "EXT-1", "a@x"]]),
                            FakeAdapter())
    with pytest.raises(ImportError, match="import_preview_changed"):
        authorize(preview, uid="owner", preview_sha256="0" * 64, now="now")
    authorized = authorize(preview, uid="owner",
                           preview_sha256=preview["preview_sha256"], now="now")
    assert authorized["preview_sha256"] == preview["preview_sha256"]


def test_import_is_idempotent_and_does_not_recreate_documents() -> None:
    adapter = FakeAdapter()
    preview = build_preview(_workspace(), _records([["Alpha", "EXT-1", "a@x"]]),
                            adapter)
    authorized = authorize(preview, uid="owner",
                           preview_sha256=preview["preview_sha256"], now="now")
    first = execute(preview, authorized=authorized, adapter=adapter, now="t1")
    assert first["status"] == "imported" and adapter.creates == 1
    second = execute(preview, authorized=authorized, adapter=adapter,
                     previous=first["rows"], now="t2")
    assert adapter.creates == 1
    assert second["rows"] == first["rows"]


def test_readback_reports_a_field_that_the_erp_did_not_keep() -> None:
    adapter = FakeAdapter()
    preview = build_preview(_workspace(), _records([["Alpha", "EXT-1", "a@x"]]),
                            adapter)
    authorized = authorize(preview, uid="owner",
                           preview_sha256=preview["preview_sha256"], now="now")
    execution = execute(preview, authorized=authorized, adapter=adapter, now="now")
    matched = readback(preview, execution, adapter)
    assert matched["passed"] is True and matched["status"] == "readback_passed"

    stored = adapter.records["res.partner"][100]
    stored["email"] = "changed@x"
    mismatched = readback(preview, execution, adapter)
    assert mismatched["passed"] is False
    assert mismatched["rows"][0]["mismatched"] == ["email"]


def test_a_retry_only_reapplies_failed_rows() -> None:
    class Flaky(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.fail_once = True

        def create(self, model, values):
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("odoo_unavailable")
            return super().create(model, values)

    adapter = Flaky()
    preview = build_preview(_workspace(), _records([["Alpha", "EXT-1", "a@x"]]),
                            adapter)
    authorized = authorize(preview, uid="owner",
                           preview_sha256=preview["preview_sha256"], now="now")
    first = execute(preview, authorized=authorized, adapter=adapter, now="t1")
    assert first["status"] == "import_partial"
    assert first["rows"][0]["status"] == "failed"
    second = execute(preview, authorized=authorized, adapter=adapter,
                     previous=first["rows"], now="t2")
    assert second["status"] == "imported"
    assert adapter.creates == 1


def test_service_keeps_mapping_confirmation_separate_from_the_import(tmp_path) -> None:
    import base64

    from forgeflow.onboarding import OnboardingError, OnboardingService

    service = OnboardingService(tmp_path / "onboarding")
    workspace = service.create("owner", "Acme", "Manufacturing", [])
    raw = base64.b64encode(
        b"name,ref,email\nAlpha,EXT-1,a@x\nBeta,EXT-2,b@x\n").decode()
    updated = service.add_source("owner", workspace["id"], "partners.csv",
                                 "text/csv", raw)
    fields = {"name": "name", "ref": "ref", "email": "email"}
    confirmed = service.confirm("owner", workspace["id"], [
        {"id": row["id"], "target_model": "res.partner",
         "target_field": fields[row["source_field"]]}
        for row in updated["mappings"]])
    # Mapping confirmation is not an import.
    assert confirmed["status"] == "mapping_confirmed"
    assert confirmed["states"]["mapping_confirmed"] is True
    assert confirmed["states"]["import_executed"] is False
    assert confirmed["import"] is None

    adapter = FakeAdapter()
    previewed = service.preview_import("owner", workspace["id"], adapter)
    preview = previewed["import"]["preview"]
    assert preview["counts"]["create"] == 2
    assert adapter.creates == 0
    with pytest.raises(OnboardingError) as unauthorized:
        service.apply_import("owner", workspace["id"],
                             preview["preview_sha256"], adapter)
    assert unauthorized.value.code == "import_not_authorized"

    service.authorize_import("owner", workspace["id"], preview["preview_sha256"])
    applied = service.apply_import("owner", workspace["id"],
                                   preview["preview_sha256"], adapter)
    assert applied["status"] == "imported"
    assert applied["states"]["import_executed"] is True
    assert adapter.creates == 2

    again = service.apply_import("owner", workspace["id"],
                                 preview["preview_sha256"], adapter)
    assert again["status"] == "imported"
    assert adapter.creates == 2  # a retry does not create a second record

    read = service.readback_import("owner", workspace["id"], adapter)
    assert read["status"] == "readback_passed"
    assert read["states"]["import_readback_passed"] is True


def test_readback_does_not_pass_while_any_row_failed_or_was_not_executed() -> None:
    class Flaky(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.fail_once = True

        def create(self, model, values):
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("odoo_unavailable")
            return super().create(model, values)

    adapter = Flaky()
    preview = build_preview(_workspace(), _records([["Alpha", "EXT-1", "a@x"]]),
                            adapter)
    authorized = authorize(preview, uid="owner",
                           preview_sha256=preview["preview_sha256"], now="now")
    partial = execute(preview, authorized=authorized, adapter=adapter, now="t1")
    assert partial["rows"][0]["status"] == "failed"
    failed = readback(preview, partial, adapter)
    assert failed["passed"] is False and failed["status"] == "readback_failed"
    assert failed["rows"][0]["readback"] == "failed"

    no_execution = readback(preview, {"rows": []}, adapter)
    assert no_execution["passed"] is False
    assert no_execution["rows"][0]["readback"] == "not_executed"

    retried = execute(preview, authorized=authorized, adapter=adapter,
                      previous=partial["rows"], now="t2")
    passed = readback(preview, retried, adapter)
    assert passed["passed"] is True and passed["status"] == "readback_passed"

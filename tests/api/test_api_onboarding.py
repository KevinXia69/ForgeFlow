"""Onboarding API: explicit workspace, mapping confirmed is not an ERP import."""
from __future__ import annotations

import base64


def _auth(client, settings) -> dict[str, str]:
    response = client.post(
        "/api/auth/session", json={"invite_code": settings.invite_codes[0]})
    assert response.status_code == 201
    return {"cookie": response.headers["set-cookie"].split(";", 1)[0]}


def _workspace_with_partner_csv(client, auth) -> dict:
    created = client.post("/api/workspaces", json={
        "name": "Acme", "industry": "Manufacturing", "modules": ["sales"]},
        headers=auth)
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    raw = base64.b64encode(
        b"name,ref,email\nAlpha,EXT-1,a@x\nBeta,EXT-2,b@x\n").decode()
    added = client.post(f"/api/workspaces/{workspace_id}/sources", json={
        "filename": "partners.csv", "media_type": "text/csv", "content": raw},
        headers=auth)
    assert added.status_code == 200, added.text
    fields = {"name": "name", "ref": "ref", "email": "email"}
    mappings = [{"id": row["id"], "target_model": "res.partner",
                 "target_field": fields[row["source_field"]]}
                for row in added.json()["mappings"]]
    confirmed = client.post(f"/api/workspaces/{workspace_id}/confirm",
                            json={"mappings": mappings}, headers=auth)
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def test_intake_requires_an_explicit_workspace(client, settings) -> None:
    auth = _auth(client, settings)
    created = client.post("/api/workspaces", json={
        "name": "Acme", "industry": "Manufacturing", "modules": []}, headers=auth)
    assert created.status_code == 201
    missing = client.post("/api/workspaces/intake", json={
        "title": "Notes", "content": "Supplier A was late."}, headers=auth)
    assert missing.status_code == 422
    accepted = client.post("/api/workspaces/intake", json={
        "workspace_id": created.json()["id"], "title": "Notes",
        "content": "Supplier A was late."}, headers=auth)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["sources"][0]["filename"] == "Notes.md"


def test_mapping_confirmation_is_not_presented_as_an_erp_import(client, settings) -> None:
    auth = _auth(client, settings)
    workspace = _workspace_with_partner_csv(client, auth)
    assert workspace["status"] == "mapping_confirmed"
    assert workspace["states"]["mapping_confirmed"] is True
    assert workspace["states"]["context_adopted"] is True
    assert workspace["states"]["import_executed"] is False
    assert workspace["states"]["import_readback_passed"] is False
    assert set(workspace["states"]) == {
        "saved", "mapping_confirmed", "context_adopted",
        "import_executed", "import_readback_passed",
    }
    assert "applied" not in workspace
    assert workspace["mapping_adoption"]["status"] == "adopted"
    assert "mapping_confirmed_not_imported" in workspace["warnings"]

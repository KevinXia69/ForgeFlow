"""Tool schemas, tool boxes and scope enforcement for tool-loop workers."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from forgeflow.superagent import (
    TOOL_SPECS,
    TOOL_SPECS_BY_NAME,
    CompositeToolBox,
    InvestigationToolBox,
    KnowledgeToolBox,
    OdooReadToolBox,
    ScopeMode,
    TenantError,
    ToolScope,
    WebSearchToolBox,
    investigation_tool_specs,
)
from forgeflow.superagent.identity import ObjectScope

ROOT = Path(__file__).resolve().parents[1]
JS_SOURCE = ROOT / "src" / "forgeflow" / "agents" / "native_investigation.js"


def _scope(*, tools=frozenset({"investigation_query", "odoo_read", "knowledge_search",
                               "web_search", "knowledge_read_source"}), object_refs=None):
    return ToolScope(
        tenant_id="tenant-a", principal_id="planner-a", ticket_id="ticket-1",
        assignment_id="assignment-1", mandate_id="mandate-1", capability="assess",
        allowed_tools=frozenset(tools),
        object_scope=(ObjectScope.of(set(object_refs))
                      if object_refs is not None else ObjectScope.all_objects()),
        budget_tokens=1000, expires_at=0.0, run_token="run-1")


def test_python_schemas_match_the_pi_extension_declarations() -> None:
    """The Python tool surface must not drift from the Pi tool registration."""
    source = JS_SOURCE.read_text(encoding="utf-8")
    declared = set(re.findall(r'name: "([a-z_]+)"', source))
    expected = {spec["name"] for spec in investigation_tool_specs()}
    assert declared, "no tool names found in the JS extension"
    assert declared == expected


def test_every_tool_spec_is_a_closed_object_schema() -> None:
    names = set()
    for spec in TOOL_SPECS:
        assert spec["name"] and spec["description"], spec
        assert spec["name"] not in names, f"duplicate tool {spec['name']}"
        names.add(spec["name"])
        parameters = spec["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert isinstance(parameters["properties"], dict)


def test_investigation_box_maps_names_to_actions_and_adds_operation_id() -> None:
    calls = []

    def dispatch(action, payload):
        calls.append((action, payload))
        return {"records": [{"entity": "purchase_order", "id": 7, "fields": {},
                             "observation_id": "obs-7"}], "complete": True,
                "next_cursor": None}

    box = InvestigationToolBox(dispatch)
    result = box.call("investigation_query",
                      {"kind": "record", "arguments": {"entity": "purchase_order",
                                                       "id": 7}},
                      scope=_scope())
    assert calls[0][0] == "query"
    assert calls[0][1]["operation_id"]
    assert result["observation_id"] == "obs-7"
    assert result["record"]["id"] == 7


def test_investigation_box_refuses_a_tool_outside_the_scope() -> None:
    box = InvestigationToolBox(lambda action, payload: {})
    with pytest.raises(Exception, match="tool_not_permitted"):
        box.call("investigation_query", {"kind": "status"},
                 scope=_scope(tools=frozenset()))


def test_odoo_read_rejects_unlisted_models_and_out_of_scope_records() -> None:
    class Adapter:
        def search_read(self, model, domain, fields, limit=100, order=None, offset=0):
            return [{"id": 7, "state": "purchase"}]

    box = OdooReadToolBox(Adapter())
    with pytest.raises(Exception, match="tool_model_not_allowed"):
        box.call("odoo_read", {"model": "res.users", "ids": [1], "fields": ["id"]},
                 scope=_scope())
    with pytest.raises(Exception, match="tool_object_scope_denied"):
        box.call("odoo_read", {"model": "purchase.order", "ids": [7], "fields": ["id"]},
                 scope=_scope(object_refs={"purchase.order:9"}))
    result = box.call("odoo_read",
                      {"model": "purchase.order", "ids": [7], "fields": ["id", "state"]},
                      scope=_scope(object_refs={"purchase.order:7"}))
    assert result["record"]["state"] == "purchase"


def test_odoo_read_drops_fields_outside_the_read_allowlist() -> None:
    captured = {}

    class Adapter:
        def search_read(self, model, domain, fields, limit=100, order=None, offset=0):
            captured["fields"] = fields
            return [{"id": 7}]

    box = OdooReadToolBox(Adapter())
    box.call("odoo_read",
             {"model": "purchase.order", "ids": [7],
              "fields": ["id", "state", "some_secret_field"]},
             scope=_scope())
    assert "some_secret_field" not in captured["fields"]


def test_knowledge_box_returns_citations_from_the_server_scope() -> None:
    captured = {}

    def answer(query, root, limit=5, scope=None):
        captured["scope"] = scope
        return {"found": True, "citations": ["document://contract.pdf#pdf:p3:l4"],
                "passages": [{"path": "document://contract.pdf",
                              "locator": "pdf:p3:l4", "text": "clause 7"}]}

    scope = object()
    box = KnowledgeToolBox(answer=answer, read_blocks=lambda **kw: [], root=".", scope=scope)
    result = box.call("knowledge_search", {"query": "contract clause"}, scope=_scope())
    assert captured["scope"] is scope
    assert result["citations"] == ["document://contract.pdf#pdf:p3:l4"]


def test_composite_box_rejects_a_duplicate_tool_name() -> None:
    box = WebSearchToolBox(lambda query: [])
    with pytest.raises(Exception, match="tool_already_registered"):
        CompositeToolBox([box, box])
    composite = CompositeToolBox([box, InvestigationToolBox(lambda a, p: {})])
    assert "web_search" in composite.names


def test_web_search_marks_results_as_untrusted() -> None:
    box = WebSearchToolBox(lambda query: [{"title": "t", "url": "https://example.com"}])
    result = box.call("web_search", {"query": "steel price"}, scope=_scope())
    assert result["trust"] == "untrusted_external"


def test_tool_scope_requires_identity_and_budget() -> None:
    with pytest.raises(TenantError, match="identity_required"):
        ToolScope(tenant_id="t", principal_id="p", ticket_id="x", assignment_id="",
                  mandate_id="m", capability="assess", allowed_tools=frozenset(),
                  object_scope=ObjectScope.all_objects(), budget_tokens=10,
                  expires_at=0.0, run_token="")
    with pytest.raises(TenantError, match="budget_required"):
        ToolScope(tenant_id="t", principal_id="p", ticket_id="x", assignment_id="a",
                  mandate_id="m", capability="assess", allowed_tools=frozenset(),
                  object_scope=ObjectScope.all_objects(), budget_tokens=0,
                  expires_at=0.0, run_token="r")


def test_empty_object_scope_denies_every_record() -> None:
    scope = ObjectScope.of(set())
    assert scope.scope_mode == ScopeMode.NONE.value
    assert scope.contains("purchase.order:7") is False
    assert ObjectScope.all_objects().contains("anything") is True


def test_tool_manifest_only_exposes_requested_tools() -> None:
    from forgeflow.superagent import tool_manifest

    manifest = tool_manifest(["odoo_read", "not_a_tool"])
    assert "odoo_read" in manifest and "not_a_tool" not in manifest
    assert "web_search" not in manifest
    assert set(TOOL_SPECS_BY_NAME) >= {"odoo_read", "web_search"}


def test_odoo_read_resolves_a_reference_instead_of_guessing_an_id() -> None:
    from forgeflow.integrations.odoo20.resolver import (
        Candidate,
        Resolution,
    )

    class Adapter:
        calls = []

        def search_read(self, model, domain, fields, limit=100, order=None, offset=0):
            Adapter.calls.append((model, domain))
            return [{"id": 900, "state": "purchase"}]

    class Resolver:
        def __init__(self, resolution):
            self.resolution = resolution

        def resolve(self, reference):
            return self.resolution

    candidate = Candidate("purchase.order", 900, "P00046", "P00046", 1, "name")
    resolution = Resolution(reference="P00046", model_hint="purchase.order",
                            candidates=[candidate])
    box = OdooReadToolBox(Adapter(), resolver=Resolver(resolution))
    result = box.call("odoo_read", {"reference": "P00046", "fields": ["id", "state"]},
                      scope=_scope(object_refs={"purchase.order:900"}))
    assert result["record"]["id"] == 900
    assert Adapter.calls[-1] == ("purchase.order", [("id", "in", [900])])


def test_an_ambiguous_reference_is_not_read_and_offers_its_candidates() -> None:
    from forgeflow.integrations.odoo20.resolver import Candidate, Resolution

    class Adapter:
        reads = 0

        def search_read(self, *args, **kwargs):
            Adapter.reads += 1
            return []

    class Resolver:
        def resolve(self, reference):
            return Resolution(reference=reference, model_hint="purchase.order",
                              candidates=[Candidate("purchase.order", 1, "PO-1", "PO-1", 1,
                                                    "name"),
                                          Candidate("purchase.order", 2, "PO-1", "PO-1", 1,
                                                    "name")])

    box = OdooReadToolBox(Adapter(), resolver=Resolver())
    result = box.call("odoo_read", {"reference": "PO-1", "fields": ["id"]},
                      scope=_scope(object_refs={"purchase.order:1", "purchase.order:2"}))
    assert result["result"]["status"] == "ambiguous"
    assert len(result["result"]["candidates"]) == 2
    assert Adapter.reads == 0


def test_out_of_scope_is_refused_before_the_record_is_read() -> None:
    class Adapter:
        reads = 0

        def search_read(self, *args, **kwargs):
            Adapter.reads += 1
            return [{"id": 7}]

    box = OdooReadToolBox(Adapter())
    with pytest.raises(Exception, match="tool_object_scope_denied"):
        box.call("odoo_read", {"model": "purchase.order", "ids": [7], "fields": ["id"]},
                 scope=_scope(object_refs={"purchase.order:9"}))
    assert Adapter.reads == 0

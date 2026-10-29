"""The main product card cannot substitute either solver margin field."""
import pytest
import test_product_local_versions as local_tests

from forgeflow.recovery import service
from forgeflow.recovery.margin import MARGIN_BASIS

system = local_tests.system
serve = local_tests.serve
workspace = local_tests.workspace
handlers = local_tests.handlers
wired = local_tests.wired


@pytest.mark.parametrize("solver_margin", [None, 0.01, 0.99])
def test_main_plan_margin_comes_from_independent_document_check(wired, monkeypatch, solver_margin):
    cases, case_id, _, connection, _, _, _ = wired
    solve = service.solve_blueprint

    def misleading_solver_fields(*args, **kwargs):
        result = solve(*args, **kwargs)
        result["plan"].update(margin=solver_margin, new_spend_margin=solver_margin)
        return result

    monkeypatch.setattr(service, "solve_blueprint", misleading_solver_fields)
    prepared = cases.prepare(case_id, local_tests.ACTOR)
    assert connection.writes == []
    assert prepared["plan"]["margin_basis"] == MARGIN_BASIS
    assert prepared["plan"]["margin"] == 93
    check = next(row for row in prepared["plan"]["checks"] if row["code"] == "margin")
    assert check["actual"] == "93%" and check["status"] == "pass"
    assert "excludes finished stock revenue" in check["source"]

from __future__ import annotations

import pytest

from forgeflow.permissions import (
    AUXILIARY_GROUPS,
    READ_ONLY_MODELS,
    WRITE_GATED_MODELS,
    parse_provision_output,
    provision_script,
)


def test_script_is_valid_python():
    script = provision_script()
    compile(script, "<provision>", "exec")


def test_script_grants_read_only_acl_for_every_model():
    script = provision_script()
    for name in READ_ONLY_MODELS:
        assert repr(name) in script
    assert '"perm_read": True' in script
    assert '"perm_write": False' in script
    assert '"perm_create": False' in script
    assert '"perm_unlink": False' in script


def test_script_checks_writes_are_refused_instead_of_only_trusting_acl_rows():
    script = provision_script()
    for operation in ("write", "create", "unlink"):
        assert f'"{operation}"' in script
    for name in WRITE_GATED_MODELS:
        assert repr(name) in script
    # Only a real AccessError counts as a denial; any other exception is a
    # failed check, not a pass.
    assert "from odoo.exceptions import AccessError" in script
    assert "error:" in script
    assert "no_check_access_api" in script
    assert 'print("SELFCHECK:" + ("ok" if not failures else ",".join(failures)))' in script


def test_script_is_idempotent_by_lookup_before_create():
    script = provision_script()
    assert 'env["res.groups"].search' in script
    assert 'env["res.users"].search([("login", "=", LOGIN)]' in script
    assert 'env["ir.model.access"].search' in script
    assert "acl.write(values)" in script
    assert "user.write(values)" in script


def test_script_adds_only_read_widening_groups():
    script = provision_script()
    assert AUXILIARY_GROUPS == (
        "api_doc.group_allow_doc", "product.group_discount_per_so_line")
    for xmlid in AUXILIARY_GROUPS:
        assert repr(xmlid) in script
    assert "except ValueError" in script


def test_script_never_embeds_a_key_or_password_for_odoo():
    script = provision_script()
    assert "RO_KEY:" in script
    # The only password is a placeholder that cannot be used to log in.
    assert "read-only-not-for-login" in script
    assert not any(len(line) > 60 and line.count(" ") == 0 for line in script.splitlines())


@pytest.mark.parametrize("days", [0, -1, 91, 1000])
def test_key_days_must_stay_within_odoo_limits(days):
    with pytest.raises(ValueError):
        provision_script(key_days=days)


@pytest.mark.parametrize("login,group", [("", "g"), ("a b", "g"), ("x", "g'y")])
def test_identifiers_must_be_simple(login, group):
    with pytest.raises(ValueError):
        provision_script(login=login, group_name=group)


def test_output_parser_reads_markers():
    text = "\n".join([
        "RO_UID:7",
        "RO_KEY:80ac8ed7bfd9827992caa92ce87add7ac33e4590",
        "SELFCHECK:ok",
    ])
    parsed = parse_provision_output(text)
    assert parsed["uid"] == 7
    assert parsed["key_present"] is True
    assert parsed["selfcheck"] == []
    assert parsed["selfcheck_seen"] is True
    assert parsed["ok"] is True


def test_missing_models_make_the_credential_unusable():
    text = "\n".join([
        "MISSING:mrp.routing.workcenter",
        "RO_UID:7",
        "RO_KEY:80ac8ed7bfd9827992caa92ce87add7ac33e4590",
        "SELFCHECK:ok",
    ])
    parsed = parse_provision_output(text)
    assert parsed["missing"] == ["mrp.routing.workcenter"]
    assert parsed["ok"] is False


def test_a_report_without_selfcheck_is_not_ok():
    parsed = parse_provision_output("RO_UID:7\nRO_KEY:0123456789abcdef\n")
    assert parsed["selfcheck_seen"] is False
    assert parsed["ok"] is False


def test_output_parser_rejects_a_failed_selfcheck():
    text = "RO_UID:7\nRO_KEY:0123456789abcdef\nSELFCHECK:write_allowed:purchase.order"
    parsed = parse_provision_output(text)
    assert parsed["ok"] is False
    assert parsed["selfcheck"] == ["write_allowed:purchase.order"]


def test_output_parser_does_not_accept_a_malformed_key():
    parsed = parse_provision_output("RO_UID:7\nRO_KEY:not a key\nSELFCHECK:ok")
    assert parsed["key_present"] is False
    assert parsed["ok"] is False


def test_read_surface_is_exactly_the_models_the_code_reads():
    # Trimmed to the 19 models the prepare/baseline/readback path touches;
    # product.template, stock.move.line, stock.warehouse, res.company, uom.uom
    # and resource.calendar are never read and must not be granted.
    assert len(READ_ONLY_MODELS) == 19
    for name in ("sale.order", "sale.order.line", "purchase.order", "purchase.order.line",
                 "mrp.production", "mrp.workorder", "mrp.workcenter", "mrp.bom",
                 "mrp.bom.line", "mrp.routing.workcenter", "product.product",
                 "product.supplierinfo", "stock.quant", "stock.move", "stock.picking",
                 "stock.route", "stock.location", "res.partner", "res.currency"):
        assert name in READ_ONLY_MODELS
    for name in ("product.template", "stock.move.line", "stock.warehouse",
                 "res.company", "uom.uom", "resource.calendar"):
        assert name not in READ_ONLY_MODELS


def test_every_read_model_is_also_write_gated():
    assert set(WRITE_GATED_MODELS) == set(READ_ONLY_MODELS)

from __future__ import annotations

import os
import uuid
import xmlrpc.client

import pytest

from forgeflow.integrations.odoo20 import Odoo20Adapter, Odoo20Config
from forgeflow.planning.rules import RuleSet, rule_from_odoo

URL = os.environ.get("FORGEFLOW_ODOO20_URL", "")
DB = os.environ.get("FORGEFLOW_ODOO20_DB", "")
LOGIN = os.environ.get("FORGEFLOW_ODOO20_LOGIN", "")
PASSWORD = os.environ.get("FORGEFLOW_ODOO20_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((URL, DB, LOGIN, PASSWORD)),
                               reason="Odoo 20 credentials not configured")


def test_live_odoo_business_rules_compile() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    assert adapter.check_contract().ok
    models = xmlrpc.client.ServerProxy(URL + "/xmlrpc/2/object")
    uid = adapter._authenticate()
    name = f"W7 test rule {uuid.uuid4().hex[:8]}"
    rule_id = None
    try:
        company = models.execute_kw(DB, uid, PASSWORD, "res.company", "search", [[]], {"limit": 1})[0]
        rule_id = models.execute_kw(DB, uid, PASSWORD, "forgeflow.business_rule", "create", [{
            "name": name, "scope_model": "mrp.production",
            "condition_json": '{"gt":{"field":"shortfall","value":0}}',
            "version": 1, "active": True, "company_id": company,
        }])
        rows = [row for row in adapter.read_business_rules("mrp.production")
                if row["name"] == name]
        assert len(rows) == 1
        hits = RuleSet([rule_from_odoo(rows[0])]).evaluate({"shortfall": 2})
        assert hits and hits[0].name == name
    finally:
        if rule_id:
            models.execute_kw(DB, uid, PASSWORD, "forgeflow.business_rule", "unlink", [[rule_id]])

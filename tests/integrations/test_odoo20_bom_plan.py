from __future__ import annotations

import os

import pytest

from forgeflow.integrations.odoo20 import Odoo20Adapter, Odoo20Config
from forgeflow.planning.multi_bom import Capacity, MultiBomPlanner
from forgeflow.planning.odoo_bom import product_from_odoo

URL = os.environ.get("FORGEFLOW_ODOO20_URL", "")
DB = os.environ.get("FORGEFLOW_ODOO20_DB", "")
LOGIN = os.environ.get("FORGEFLOW_ODOO20_LOGIN", "")
PASSWORD = os.environ.get("FORGEFLOW_ODOO20_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((URL, DB, LOGIN, PASSWORD)),
                               reason="Odoo 20 credentials not configured")


def test_live_odoo_bom_feeds_cp_sat() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    rows = adapter.search_read("product.product", [("name", "=", "W7 BOM Root")],
                               ["id", "name"], limit=1)
    assert rows
    tree = adapter.read_bom_tree(rows[0]["id"])
    assert tree is not None and tree["lines"]
    product = product_from_odoo(tree, code="W7", demand=10, resource_hours={"press": 1.0})
    result = MultiBomPlanner([product], [Capacity("press", 20)]).solve()
    assert result.produced["W7"] == 10
    component = tree["lines"][0]["product_id"]
    assert result.component_need[str(component)] == 20

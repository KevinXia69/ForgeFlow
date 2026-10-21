from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "brand_dsh_web.py"
if not SCRIPT.is_file():
    pytest.skip("brand patch is local-runtime only", allow_module_level=True)
spec = importlib.util.spec_from_file_location("brand_dsh_web", SCRIPT)
brand = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(brand)


def test_brand_patch_replaces_sidebar_and_hero_without_jsx(tmp_path):
    bundle = tmp_path / "client.js"
    bundle.write_text(
        'function OfficialBrandMark({ size }) {\n'
        '\t\t\treturn require("x").FishLogo({ size });\n'
        '}\n'
        'function OfficialBrandName() {\n'
        '\t\t\treturn require("x").BrandWordmark({ includeMark: false });\n'
        '}\n'
        '\t\t//#region lib/types/client/index.js\n'
        '\t\tfunction apply(ctx) {\n'
        '\t\t\tctx.slots.inject("sidebar.brand.mark", () => null);\n'
        '}\n',
        encoding="utf-8",
    )
    assert brand._patch_brand_bundle(bundle)
    text = bundle.read_text(encoding="utf-8")
    assert "ForgeFlow" in text
    assert "ForgeFlowHeroMark" in text
    assert "conversation.hero.brand.mark" in text
    assert "<span" not in text

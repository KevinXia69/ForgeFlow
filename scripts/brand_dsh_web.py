#!/usr/bin/env python3
"""Replace the installed DSH Web shell's visible brand with ForgeFlow.

This edits only the local, ignored DSH runtime (not this repository): the
official brand client bundle, the static HTML title/favicon, and the PWA
manifest. It is idempotent and safe to run after every DSH install/update.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "web" / "src" / "assets" / "forgeflow-logo.png"
TITLE = "ForgeFlow"
MARK = (
    '(0, react_jsx_runtime.jsx)("span", { "aria-hidden": true, style: {'
    'display: "inline-grid", placeItems: "center", width: size, height: size, '
    'borderRadius: Math.max(6, Math.round(size * 0.24)), background: "#2856d8", '
    'color: "#fff", fontWeight: 800, fontFamily: "system-ui, sans-serif", '
    'fontSize: Math.round(size * 0.62) }, children: "F" })'
)
NAME = (
    '(0, react_jsx_runtime.jsx)("span", { style: { fontFamily: "Georgia, serif", '
    'fontWeight: 600, letterSpacing: "-0.02em" }, children: "ForgeFlow" })'
)


def _runtime_node_modules(cli: Path) -> Path:
    # <node_modules>/@deepseek-ai/dsh/lib/bin.js
    return cli.resolve().parents[3]


BRAND_BUNDLE = """window.__ModuleLoader__.load({
	id: "@deepseek-ai/dsh-client-ui-brand-official",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react_jsx_runtime = require("react/jsx-runtime");
		function ForgeFlowMark({ size }) {
			return (0, react_jsx_runtime.jsx)("img", {
				src: "./forgeflow-logo.png", alt: "", width: size, height: size,
				style: { objectFit: "contain", display: "block" },
			});
		}
		function ForgeFlowName() {
			return (0, react_jsx_runtime.jsx)("span", {
				style: { fontFamily: "Geist, system-ui, sans-serif", fontWeight: 600, letterSpacing: "-0.02em" },
				children: "ForgeFlow",
			});
		}
		function ForgeFlowHeroMark({ size }) { return ForgeFlowMark({ size }); }
		const inject = ["slots"];
		function apply(ctx) {
			if (!document.getElementById("forgeflow-font")) {
				const style = document.createElement("style");
				style.id = "forgeflow-font";
				style.textContent = "@font-face{font-family:Geist;src:url('./forgeflow-geist.woff2') format('woff2');font-weight:100 900;font-style:normal;font-display:swap}:root{--dsh-font-family-text:Geist,system-ui,sans-serif;--dsh-font-family-text-default:Geist,system-ui,sans-serif}";
				document.head.append(style);
			}
			ctx.slots.inject("conversation.hero.brand.mark", () => ctx.slots.register({ name: "conversation.hero.brand.mark" }, ForgeFlowHeroMark));
			ctx.slots.inject("sidebar.brand.mark", () => ctx.slots.inject("sidebar.brand.name", function* () {
				yield ctx.slots.register({ name: "sidebar.brand.mark" }, ForgeFlowMark);
				yield ctx.slots.register({ name: "sidebar.brand.name" }, ForgeFlowName);
			}));
		}
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});
"""


def _patch_brand_bundle(path: Path) -> bool:
    if path.read_text(encoding="utf-8") == BRAND_BUNDLE:
        return False
    path.write_text(BRAND_BUNDLE, encoding="utf-8")
    return True


def _patch_package(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    inject = data.setdefault("dsh", {}).setdefault("client", {}).setdefault("inject", [])
    if "@deepseek-ai/dsh-client-ui-conversation" not in inject:
        inject.append("@deepseek-ai/dsh-client-ui-conversation")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _patch_frontend(dist: Path) -> None:
    index = dist / "index.html"
    if index.is_file():
        text = index.read_text(encoding="utf-8")
        text = re.sub(r"<title>[^<]*</title>", f"<title>{TITLE}</title>", text, count=1)
        text = re.sub(
            r'href="\./favicon[^"]*\.svg"',
            'href="./forgeflow.svg"',
            text,
        )
        index.write_text(text, encoding="utf-8")
    manifest = dist / "manifest.webmanifest"
    if manifest.is_file():
        manifest.write_text(
            json.dumps({
                "name": TITLE, "short_name": TITLE, "start_url": "./", "scope": "./",
                "display": "fullscreen",
                "icons": [{"src": "forgeflow.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
            }, indent=2) + "\n",
            encoding="utf-8",
        )
    if LOGO.is_file():
        shutil.copy2(LOGO, dist / "forgeflow-logo.png")
    geist = ROOT / "web" / "node_modules" / "@fontsource-variable" / "geist" / "files" / "geist-latin-wght-normal.woff2"
    if geist.is_file():
        shutil.copy2(geist, dist / "forgeflow-geist.woff2")
    (dist / "forgeflow.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="15" fill="#2856d8"/>'
        '<path d="M18 17h30v8H28v8h16v8H28v14h-10z" fill="#fff"/></svg>\n',
        encoding="utf-8",
    )


def main() -> int:
    cli = os.environ.get("FORGEFLOW_DSH_CLI", "").strip()
    if not cli:
        print("FORGEFLOW_DSH_CLI is required", file=os.sys.stderr)
        return 2
    modules = _runtime_node_modules(Path(cli))
    brand = modules / "@deepseek-ai" / "dsh-client-ui-brand-official"
    frontend = modules / "@deepseek-ai" / "dsh-web-frontend" / "dist"
    if not (brand / "lib/client.js").is_file() or not frontend.is_dir():
        print("DSH Web brand assets were not found", file=os.sys.stderr)
        return 2
    _patch_package(brand / "package.json")
    changed = _patch_brand_bundle(brand / "lib/client.js")
    _patch_frontend(frontend)
    print(f"DSH Web brand: ForgeFlow ({'patched' if changed else 'already patched'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

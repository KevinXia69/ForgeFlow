/**
 * v14 E20: colour contrast, measured rather than asserted by eye.
 *
 * It serves the built bundle, walks every visible text node, computes the
 * contrast ratio between the text colour and the effective background, and
 * reports anything below WCAG AA (4.5:1 for body text, 3:1 for large text).
 *
 * Large text is >= 24px, or >= 18.66px when bold, per WCAG 1.4.3.
 *
 * What this does NOT measure: a real screen reader. That stays a boundary, not
 * a claim. Focus indicators, motion and zoom are covered by other harnesses.
 */
import { spawn } from "node:child_process";
import { mkdtemp, rm, symlink, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const which = process.argv[2] === "public" ? "public" : "private";
const approved = JSON.parse(await readFile(fileURLToPath(new URL("../../web/e2e/fixtures/approved-case.json", import.meta.url)), "utf8"));

let root;
let temporary = null;
if (which === "public") {
  temporary = await mkdtemp(join(tmpdir(), "forgeflow-contrast-"));
  await symlink(fileURLToPath(new URL("../../web-public/dist", import.meta.url)), join(temporary, "app"), "dir");
  root = temporary;
} else {
  root = fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
}

const port = Number(process.env.CONTRAST_PORT ?? (which === "public" ? 4175 : 4174));
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise((resolve) => setTimeout(resolve, 600));

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: which === "public" ? "en-US" : "zh-CN" });

await page.route("**/api/**", (route) => {
  const path = new URL(route.request().url()).pathname;
  const reply = (json, status = 200) => route.fulfill({ status, json });
  if (path === "/api/auth/me") return reply({ scope_id: "a11y", display_name: "Operator", deployment: which === "public" ? "review" : "local" });
  if (path === "/api/config") return reply({ deployment: which === "public" ? "review" : "local", runner_enabled: false });
  if (path === "/api/capabilities") return reply({ case_read: true, case_qa: true, case_create: false, case_prepare: false, case_approve: false, product_approval: false, environment: { execution: "unavailable", ready: false } });
  if (path === "/api/product/cases") return reply([approved]);
  if (path.startsWith("/api/product/cases/")) return reply(approved);
  if (path === "/api/runs") return reply([]);
  return reply({});
});

const AUDIT = `(() => {
  const parse = (value) => {
    const match = /rgba?\\(([^)]+)\\)/.exec(value || "");
    if (!match) return null;
    const parts = match[1].split(",").map((part) => Number.parseFloat(part.trim()));
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const luminance = ({ r, g, b }) => {
    const channel = (value) => {
      const scaled = value / 255;
      return scaled <= 0.03928 ? scaled / 12.92 : Math.pow((scaled + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  };
  const blend = (front, back) => ({
    r: front.r * front.a + back.r * (1 - front.a),
    g: front.g * front.a + back.g * (1 - front.a),
    b: front.b * front.a + back.b * (1 - front.a),
    a: 1,
  });
  const backgroundOf = (node) => {
    let element = node.nodeType === 3 ? node.parentElement : node;
    const layers = [];
    while (element) {
      const colour = parse(getComputedStyle(element).backgroundColor);
      if (colour && colour.a > 0) layers.push(colour);
      if (colour && colour.a === 1) break;
      element = element.parentElement;
    }
    let result = { r: 255, g: 255, b: 255, a: 1 };
    for (let index = layers.length - 1; index >= 0; index -= 1) result = blend(layers[index], result);
    return result;
  };
  const ratio = (a, b) => {
    const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (light + 0.05) / (dark + 0.05);
  };
  const results = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = (node.nodeValue || "").trim();
    if (!text) continue;
    const parent = node.parentElement;
    if (!parent || seen.has(parent)) continue;
    if (parent.closest('script, style, [hidden], [aria-hidden="true"], details:not([open])')) continue;
    const box = parent.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) continue;
    const style = getComputedStyle(parent);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) continue;
    seen.add(parent);
    const foreground = parse(style.color);
    if (!foreground) continue;
    const background = backgroundOf(node);
    const painted = foreground.a < 1 ? blend(foreground, background) : foreground;
    const size = Number.parseFloat(style.fontSize);
    const bold = Number(style.fontWeight) >= 700;
    const large = size >= 24 || (bold && size >= 18.66);
    results.push({
      text: text.slice(0, 40),
      selector: parent.tagName.toLowerCase() + (parent.className ? "." + String(parent.className).split(" ")[0] : ""),
      size: Math.round(size * 10) / 10,
      ratio: Math.round(ratio(painted, background) * 100) / 100,
      required: large ? 3 : 4.5,
    });
  }
  return results;
})()`;

const pages = which === "public"
  ? [["start", ""], ["case", `#case=${approved.case_id}`], ["evidence", "#runs"]]
  : [["start", ""], ["case", `#case=${approved.case_id}`], ["evidence", "#runs"]];

let failures = 0;
let checked = 0;
try {
  for (const [label, hash] of pages) {
    await page.goto(`http://127.0.0.1:${port}/app/${hash}`, { waitUntil: "domcontentloaded" });
    const marker = label === "start" ? "[data-home-view]" : label === "case" ? "[data-case-detail]" : "[data-runs-list]";
    await page.locator(marker).waitFor({ timeout: 20000 }).catch(() => console.log(`  note: ${label} did not show ${marker}`));
    await page.waitForTimeout(900);
    const rows = await page.evaluate(AUDIT);
    if (rows.length < 10) {
      console.log(`${which}/${label}: only ${rows.length} text runs — this is not the rendered product`);
      failures += 1;
      continue;
    }
    checked += rows.length;
    const bad = rows.filter((row) => row.ratio < row.required);
    failures += bad.length;
    const worst = rows.slice().sort((a, b) => a.ratio - b.ratio)[0];
    console.log(`${which}/${label}: ${rows.length} text runs, worst ${worst ? `${worst.ratio}:1 (${worst.selector} ${worst.size}px)` : "—"}, below AA: ${bad.length}`);
    for (const row of bad.slice(0, 6)) {
      console.log(`  FAIL ${row.ratio}:1 < ${row.required} (${row.size}px) ${row.selector} — "${row.text}"`);
    }
  }
} finally {
  await browser.close();
  server.kill();
  if (temporary) await rm(temporary, { recursive: true, force: true });
}

console.log(`contrast: ${checked - failures}/${checked} text runs meet WCAG AA`);
console.log("contrast=not-measured: screen reader");
process.exitCode = failures ? 1 : 0;

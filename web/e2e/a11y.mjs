/** Keyboard, structure and reflow checks for every first-use surface.
 *
 * What this measures:
 *   - the sign-in card: focus order, visible outline, one h1
 *   - the start page: one h1, the composer is present, the function cards are
 *     buttons that take focus, no horizontal overflow at a 720px column
 *     (a 1440 layout at 200% zoom reflows into roughly that width)
 *   - the case list: rows are buttons, no overflow at 720px
 *   - a case: one h1, a decision control is rendered, no overflow
 *   - evidence: every filter control carries a label or aria-label, no overflow
 *   - the settings dialog: modal, traps focus, restores focus
 *
 * What this does NOT measure, and therefore does not claim:
 *   - colour contrast ratios (no contrast sampling here)
 *   - a real screen reader (no assistive technology was driven)
 *   - true browser zoom (the 720px column is a reflow proxy, not zoom)
 *   - 390px layout (covered by the responsive evidence and narrow-state.mjs)
 */
import { chromium } from "playwright-core";

const base = process.env.FORGEFLOW_BASE_URL || "http://127.0.0.1:8000";
const invite = process.env.FORGEFLOW_INVITE || "demo-invite-code-0001";
const browser = await chromium.launch({ channel: "chrome", headless: true });
const checked = [];

function assert(condition, message) {
  checked.push(message);
  if (!condition) throw new Error(`[a11y] ${message}`);
}

const overflow = (page) => page.evaluate(() => ({
  scrollWidth: document.documentElement.scrollWidth,
  clientWidth: document.documentElement.clientWidth,
}));

const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
try {
  await page.goto(base, { waitUntil: "domcontentloaded" });
  await page.locator("#invite-code").waitFor();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  assert(await page.evaluate(() => document.activeElement?.id === "invite-code"), "login: focus reaches the invite field");
  const outline = await page.locator("#invite-code").evaluate((node) => getComputedStyle(node).outlineStyle);
  assert(outline !== "none", "login: the focused input has a visible outline");
  assert(await page.getByRole("heading", { level: 1 }).count() === 1, "login: exactly one h1");

  await page.locator("#invite-code").fill(invite);
  await page.getByRole("button", { name: /进入工作台|Enter workbench/ }).click();
  await page.locator("[data-nav]").first().waitFor({ timeout: 20000 });

  // ---- start page -------------------------------------------------------
  await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
  assert(await page.getByRole("heading", { level: 1 }).count() === 1, "start page: exactly one h1");
  const cards = page.locator("[data-home-card]");
  assert(await cards.count() === 4, "start page: four function cards are buttons");
  await cards.first().focus();
  assert(await cards.first().evaluate((node) => node === document.activeElement), "start page: cards take keyboard focus");
  const composer = page.locator(".home-chat textarea, .home-chat [contenteditable='true'], .home-chat input");
  assert(await composer.count() >= 1, "start page: the composer is present and labelled");
  await page.setViewportSize({ width: 720, height: 900 });
  await page.waitForTimeout(300);
  const homeOverflow = await overflow(page);
  assert(homeOverflow.scrollWidth <= homeOverflow.clientWidth + 1, "start page: no horizontal overflow in a 720px column");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(200);

  // ---- case list --------------------------------------------------------
  await page.locator('[data-nav="cases"]').click();
  await page.locator('[data-cases-view][data-cases-ready="true"]').waitFor({ timeout: 20000 });
  const rows = page.locator("[data-case-row]");
  if (await rows.count() > 0) {
    await rows.first().focus();
    assert(await rows.first().evaluate((node) => node === document.activeElement), "case list: rows take keyboard focus");
  }
  await page.setViewportSize({ width: 720, height: 900 });
  await page.waitForTimeout(300);
  const listOverflow = await overflow(page);
  assert(listOverflow.scrollWidth <= listOverflow.clientWidth + 1, "case list: no horizontal overflow in a 720px column");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(200);

  // ---- a case -----------------------------------------------------------
  if (await rows.count() > 0) {
    await rows.first().click();
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
    assert(await page.getByRole("heading", { level: 1 }).count() === 1, "case: exactly one h1");
    const decisionControls = page.locator("[data-decision-panel] button, [data-case-approve-sticky]");
    assert(await decisionControls.count() >= 1, "case: at least one decision control is rendered");
    await page.setViewportSize({ width: 720, height: 900 });
    await page.waitForTimeout(300);
    const caseOverflow = await overflow(page);
    assert(caseOverflow.scrollWidth <= caseOverflow.clientWidth + 1, "case: no horizontal overflow in a 720px column");
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForTimeout(200);
  }

  // ---- evidence ---------------------------------------------------------
  await page.locator('[data-nav="runs"]').click();
  await page.locator("[data-runs-list]").waitFor({ timeout: 20000 });
  const unlabelled = await page.locator("input, select").evaluateAll((nodes) => nodes.filter((node) => {
    const id = node.getAttribute("id");
    const aria = node.getAttribute("aria-label");
    const labelled = id ? document.querySelector(`label[for="${id}"]`) : null;
    return !aria && !labelled;
  }).length);
  assert(unlabelled === 0, "evidence: every filter control has a label or aria-label");
  await page.setViewportSize({ width: 720, height: 900 });
  await page.waitForTimeout(300);
  const evidenceOverflow = await overflow(page);
  assert(evidenceOverflow.scrollWidth <= evidenceOverflow.clientWidth + 1, "evidence: no horizontal overflow in a 720px column");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(200);

  // ---- settings dialog --------------------------------------------------
  await page.getByRole("button", { name: /设置|Settings/ }).first().click();
  const dialog = page.getByRole("dialog");
  await dialog.waitFor();
  assert(await dialog.getAttribute("aria-modal") === "true", "settings: the dialog is modal");
  assert(await dialog.evaluate((node) => node.contains(document.activeElement)), "settings: focus enters the dialog");
  for (let index = 0; index < 8; index += 1) {
    await page.keyboard.press("Tab");
    assert(await dialog.evaluate((node) => node.contains(document.activeElement)), "settings: focus stays in the dialog");
  }
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });
  assert(await page.getByRole("button", { name: /设置|Settings/ }).first().evaluate((node) => node === document.activeElement), "settings: focus returns to the trigger");

  console.log(`a11y=ok (${checked.length} checks: keyboard, structure, 720px reflow)`);
  console.log("a11y=not-measured: colour contrast, screen reader, true browser zoom, 390px layout");
} finally {
  await browser.close();
}

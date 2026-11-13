/**
 * One place that knows how to reach a view.
 *
 * Scripts used to repeat `[data-nav="cases"]` plus a per-view readiness wait,
 * so any navigation change meant editing five files. They now call gotoView()
 * and the readiness selector lives here.
 */
export const VIEWS = ["home", "cases", "runs"];

const READY = {
  home: "[data-home-view]",
  cases: '[data-cases-view][data-cases-ready="true"]',
  runs: "[data-runs-list]",
};

/** Click into a view and wait until it is usable. */
export async function gotoView(page, view) {
  const selector = READY[view];
  if (!selector) throw new Error(`unknown view: ${view}`);
  if (view === "home") {
    await page.locator('[data-nav="knowledge"]').click();
  } else if (view === "runs") {
    // Saved execution evidence has a route but no top-level sidebar button.
    await page.evaluate(() => { location.hash = "#runs"; });
  } else {
    await page.locator(`[data-nav="${view}"]`).click();
  }
  await page.locator(selector).waitFor({ timeout: 20000 });
  if (view === "runs") {
    const filters = page.locator('details.evidence-filters-more');
    if (await filters.count() && await filters.first().getAttribute("open") === null) {
      await filters.first().locator('summary').click();
    }
  }
}

/**
 * Assert what the start page promises: it is the default view, it carries the
 * four function cards and the composer, and it is not the case list.
 */
export async function assertStartPage(page, assert) {
  await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
  assert(await page.locator('[data-nav="knowledge"]').getAttribute("aria-current") === "page", "start page is not the default view");
  assert(await page.locator("[data-case-row]").count() === 0, "case rows rendered on the start page");
  assert(await page.locator("[data-home-view] .chat-preset").count() === 4, "start page does not carry four function cards");
  assert(await page.locator("[data-home-view] [data-composer-input]").count() === 1, "start page does not provide a composer");
}

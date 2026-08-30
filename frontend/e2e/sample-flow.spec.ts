/**
 * The canonical happy path: Load sample -> Run -> 22 sites (golden MultiRM output),
 * table <-> track selection, and the alpha threshold.
 */
import { expect, test } from "@playwright/test";
import {
  GOLDEN,
  SAMPLE,
  armLoadingObserver,
  collectAllRows,
  fixtureSite,
  glyph,
  keyOf,
  loadSample,
  loadSampleAndRun,
  pText,
  row,
  runAndWait,
  sawLoading,
} from "./helpers";

/** Rows listed in the brief as the canonical ground truth. */
const CANONICAL_KEYS = ["52:Gm", "63:m5C", "68:m5U", "69:m1A", "79:Cm", "79:m5C"];
/** Positions carrying several modification types at once. */
const MULTI_MOD_KEYS = ["79:Cm", "79:m5C", "123:Um", "123:m5U", "123:Psi"];

test.describe("sample flow", () => {
  test("home page loads with the sequence tool and the license notice", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/RModHub/);
    await expect(page.getByTestId("sequence-input")).toBeVisible();
    await expect(page.getByTestId("load-sample")).toBeVisible();
    await expect(page.getByTestId("run")).toBeDisabled();
    const footer = page.getByTestId("footer-license");
    await expect(footer).toBeVisible();
    await expect(footer).toContainText("MIT");
    await expect(footer).toContainText("MultiRM");
  });

  test("Load sample fills the 151-nt sequence", async ({ page }) => {
    await page.goto("/");
    await loadSample(page);
    expect(SAMPLE.sequence).toHaveLength(151);
    await expect(page.getByTestId("sequence-length")).toContainText("151 nt");
    await expect(page.getByTestId("local-error")).toHaveCount(0);
    await expect(page.getByTestId("run")).toBeEnabled();
  });

  test("Run shows the loading state, then 22 sites in the summary, table and track", async ({ page }) => {
    await page.goto("/");
    await loadSample(page);
    await armLoadingObserver(page);
    await page.getByTestId("run").click();
    await expect(page.getByTestId("results")).toBeVisible();
    expect(await sawLoading(page), "the loading indicator was shown during the request").toBe(true);
    await expect(page.getByTestId("loading")).toHaveCount(0);
    await expect(page.getByTestId("error")).toHaveCount(0);
    await expect(page.getByTestId("empty")).toHaveCount(0);

    // Summary
    expect(GOLDEN.meta.n_sites).toBe(22);
    await expect(page.getByTestId("summary")).toBeVisible();
    await expect(page.getByTestId("n-sites")).toHaveText(String(GOLDEN.meta.n_sites));

    // Table: every fixture row, once, and nothing else.
    const rows = await collectAllRows(page);
    expect(rows).toHaveLength(GOLDEN.results.length);
    const keys = rows.map((r) => r.key);
    expect(new Set(keys)).toEqual(new Set(GOLDEN.results.map(keyOf)));
    for (const key of MULTI_MOD_KEYS) expect(keys).toContain(key);
    for (const r of rows) {
      const site = fixtureSite(r.key);
      expect(r.position).toBe(site.position);
      expect(r.modType).toBe(site.mod_type);
    }

    // The canonical rows show their p-values (4 decimals).
    for (const key of CANONICAL_KEYS) {
      await expect(row(page, key)).toContainText(pText(fixtureSite(key)));
    }
    await expect(page.getByTestId("visible-count")).toHaveText(
      `Showing ${GOLDEN.results.length} of ${GOLDEN.results.length} sites`,
    );

    // Track view: one glyph per site.
    const track = page.getByTestId("track-view");
    await expect(track).toBeVisible();
    await expect(track.getByTestId("track-site")).toHaveCount(GOLDEN.results.length);
    for (const key of MULTI_MOD_KEYS) await expect(glyph(page, key)).toHaveCount(1);
  });

  test("selecting a row highlights its glyph and attention windows; clicking the glyph deselects", async ({ page }) => {
    await loadSampleAndRun(page);
    const key = "52:Gm";

    await row(page, key).click();
    await expect(row(page, key)).toHaveAttribute("aria-selected", "true");
    await expect(glyph(page, key)).toHaveAttribute("data-selected", "true");
    await expect(page.getByTestId("track-attention").first()).toBeAttached();
    await expect(page.locator('[data-testid="track-site"][data-selected="true"]')).toHaveCount(1);

    await glyph(page, key).click();
    await expect(glyph(page, key)).toHaveAttribute("data-selected", "false");
    await expect(row(page, key)).toHaveAttribute("aria-selected", "false");
    await expect(page.locator('[data-testid="track-site"][data-selected="true"]')).toHaveCount(0);
    // Move the pointer off the glyph: attention is shown for the selected OR hovered site.
    await page.mouse.move(0, 0);
    await expect(page.getByTestId("track-attention")).toHaveCount(0);
  });

  test("alpha is applied server-side: 0.01 keeps only the sites with p < 0.01", async ({ page }) => {
    await page.goto("/");
    await loadSample(page);
    await page.getByTestId("alpha-input").fill("0.01");
    await expect(page.getByTestId("alpha-input")).toHaveValue("0.01");
    await runAndWait(page);
    const expected = GOLDEN.results.filter((s) => (s.p_value ?? 1) < 0.01);
    expect(expected.length).toBeGreaterThan(0);
    await expect(page.getByTestId("n-sites")).toHaveText(String(expected.length));
    const rows = await collectAllRows(page);
    expect(new Set(rows.map((r) => r.key))).toEqual(new Set(expected.map(keyOf)));
  });
});

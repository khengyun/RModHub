/**
 * Results table: modification-type chips, numeric/text filters, sorting, pagination.
 * Every expected count is derived from the golden fixture.
 */
import { expect, test, type Page } from "@playwright/test";
import {
  GOLDEN,
  MANY_ROWS,
  MOD_TYPES,
  asciiCmp,
  collectAllRows,
  fillSequence,
  isMonotonic,
  keyOf,
  loadSampleAndRun,
  localeCmp,
  numberCmp,
  readRows,
  runAndWait,
  selectPageSize,
  sortHeader,
  type SortColumn,
  type SortDirection,
} from "./helpers";

const N = GOLDEN.results.length; // 22
const pOf = (s: { p_value: number | null }) => s.p_value ?? Number.NaN;

/**
 * Leave only `keep` selected by toggling every other *active* chip off. Chips of
 * modification types with zero rows are rendered disabled, so they are skipped.
 */
async function keepOnlyModType(page: Page, keep: (typeof MOD_TYPES)[number]): Promise<void> {
  for (const mt of MOD_TYPES) {
    if (mt === keep) continue;
    const chip = page.getByTestId(`filter-mod-type-${mt}`);
    await expect(chip).toBeAttached();
    if (await chip.isDisabled()) continue;
    if ((await chip.getAttribute("aria-pressed")) === "false") continue;
    await chip.click();
    await expect(chip).toHaveAttribute("aria-pressed", "false");
  }
  await expect(page.getByTestId(`filter-mod-type-${keep}`)).toHaveAttribute("aria-pressed", "true");
}

async function currentDirection(page: Page, column: SortColumn): Promise<SortDirection> {
  const dir = await sortHeader(page, column).getAttribute("aria-sort");
  expect(["ascending", "descending"]).toContain(dir);
  return dir as SortDirection;
}

test.describe("filters, sorting and pagination on the 22-site sample", () => {
  test.beforeEach(async ({ page }) => {
    await loadSampleAndRun(page);
    await expect(page.getByTestId("n-sites")).toHaveText(String(N));
  });

  test("modification-type chips narrow the table and the track view", async ({ page }) => {
    const m5c = GOLDEN.results.filter((s) => s.mod_type === "m5C");
    expect(m5c.length).toBeGreaterThan(0);

    await keepOnlyModType(page, "m5C");
    await expect(page.getByTestId("visible-count")).toHaveText(`Showing ${m5c.length} of ${N} sites`);
    const rows = await collectAllRows(page);
    expect(rows).toHaveLength(m5c.length);
    expect(rows.every((r) => r.modType === "m5C")).toBe(true);
    expect(new Set(rows.map((r) => r.key))).toEqual(new Set(m5c.map(keyOf)));
    // The track view draws the filtered rows only.
    await expect(page.getByTestId("track-view").getByTestId("track-site")).toHaveCount(m5c.length);

    await page.getByTestId("filter-reset").click();
    await expect(page.getByTestId("visible-count")).toHaveText(`Showing ${N} of ${N} sites`);
    expect(await collectAllRows(page)).toHaveLength(N);
  });

  test("p-value, probability, position and text filters; reset restores all rows", async ({ page }) => {
    // p <= 0.03
    const pMax = 0.03;
    const byP = GOLDEN.results.filter((s) => pOf(s) <= pMax);
    expect(byP.length).toBeGreaterThan(0);
    expect(byP.length).toBeLessThan(N);
    await page.getByTestId("filter-pvalue-max").fill(String(pMax));
    await expect(page.getByTestId("visible-count")).toHaveText(`Showing ${byP.length} of ${N} sites`);
    expect(new Set((await collectAllRows(page)).map((r) => r.key))).toEqual(new Set(byP.map(keyOf)));

    await page.getByTestId("filter-reset").click();
    await expect(page.getByTestId("visible-count")).toHaveText(`Showing ${N} of ${N} sites`);

    // probability >= 0.5
    const probMin = 0.5;
    const byProb = GOLDEN.results.filter((s) => s.probability >= probMin);
    expect(byProb.length).toBeGreaterThan(0);
    await page.getByTestId("filter-prob-min").fill(String(probMin));
    await expect(page.getByTestId("visible-count")).toHaveText(`Showing ${byProb.length} of ${N} sites`);
    expect(new Set((await collectAllRows(page)).map((r) => r.key))).toEqual(new Set(byProb.map(keyOf)));

    await page.getByTestId("filter-reset").click();

    // 70 <= position <= 80
    const [posMin, posMax] = [70, 80];
    const byPos = GOLDEN.results.filter((s) => s.position >= posMin && s.position <= posMax);
    expect(byPos.length).toBeGreaterThan(0);
    await page.getByTestId("filter-pos-min").fill(String(posMin));
    await page.getByTestId("filter-pos-max").fill(String(posMax));
    await expect(page.getByTestId("visible-count")).toHaveText(`Showing ${byPos.length} of ${N} sites`);
    expect(new Set((await collectAllRows(page)).map((r) => r.key))).toEqual(new Set(byPos.map(keyOf)));

    await page.getByTestId("filter-reset").click();

    // free text "Gm" matches the Gm rows only
    const byText = GOLDEN.results.filter((s) => s.mod_type.includes("Gm"));
    expect(byText.length).toBeGreaterThan(0);
    await page.getByTestId("filter-text").fill("Gm");
    await expect(page.getByTestId("visible-count")).toHaveText(`Showing ${byText.length} of ${N} sites`);
    expect(new Set((await collectAllRows(page)).map((r) => r.key))).toEqual(new Set(byText.map(keyOf)));

    await page.getByTestId("filter-reset").click();
    await expect(page.getByTestId("filter-text")).toHaveValue("");
    await expect(page.getByTestId("visible-count")).toHaveText(`Showing ${N} of ${N} sites`);
    expect(await collectAllRows(page)).toHaveLength(N);
  });

  test("sort buttons toggle ascending/descending and expose aria-sort", async ({ page }) => {
    const ps = GOLDEN.results.map(pOf);
    const minP = Math.min(...ps).toFixed(4);
    const maxP = Math.max(...ps).toFixed(4);
    const first = page.getByTestId("result-row").first();

    // p-value: first click ascending (smallest p first), second click descending.
    await page.getByTestId("sort-p_value").click();
    await expect(sortHeader(page, "p_value")).toHaveAttribute("aria-sort", "ascending");
    await expect(first).toContainText(minP);
    await page.getByTestId("sort-p_value").click();
    await expect(sortHeader(page, "p_value")).toHaveAttribute("aria-sort", "descending");
    await expect(first).toContainText(maxP);

    // mod_type: rows grouped in one consistent collation — the backend's canonical
    // order (Am, Cm, Gm, Um, m1A, ...), ASCII, or locale.
    await page.getByTestId("sort-mod_type").click();
    const modDir = await currentDirection(page, "mod_type");
    const mods = (await readRows(page)).map((r) => r.modType);
    expect(mods).toHaveLength(N);
    const canonicalCmp = (a: string, b: string) =>
      MOD_TYPES.indexOf(a as (typeof MOD_TYPES)[number]) - MOD_TYPES.indexOf(b as (typeof MOD_TYPES)[number]);
    expect(
      isMonotonic(mods, canonicalCmp, modDir) ||
        isMonotonic(mods, asciiCmp, modDir) ||
        isMonotonic(mods, localeCmp, modDir),
      `mod types sorted ${modDir}: ${mods.join(",")}`,
    ).toBe(true);

    // position after mod_type: the sort column switches and only one header is sorted.
    await page.getByTestId("sort-position").click();
    const posDir = await currentDirection(page, "position");
    await expect(sortHeader(page, "mod_type")).not.toHaveAttribute("aria-sort", /ascending|descending/);
    const positions = (await readRows(page)).map((r) => r.position);
    expect(isMonotonic(positions, numberCmp, posDir)).toBe(true);
    const extreme = posDir === "ascending" ? Math.min : Math.max;
    expect(positions[0]).toBe(extreme(...GOLDEN.results.map((s) => s.position)));

    // probability
    await page.getByTestId("sort-probability").click();
    const probDir = await currentDirection(page, "probability");
    const probs = GOLDEN.results.map((s) => s.probability);
    const topKey = keyOf(
      GOLDEN.results[probs.indexOf(probDir === "ascending" ? Math.min(...probs) : Math.max(...probs))],
    );
    await expect(first).toHaveAttribute("data-key", topKey);
  });

  test("page size selector: 25/50/100/250, default 50", async ({ page }) => {
    const select = page.getByTestId("page-size");
    await expect(select).toHaveValue("50");
    await expect(select.locator("option")).toHaveText(["25", "50", "100", "250"]);
    await expect(page.getByTestId("page-info")).toHaveText(/Page 1 of 1/);
    await expect(page.getByTestId("result-row")).toHaveCount(N);

    await selectPageSize(page, 25);
    await expect(page.getByTestId("page-info")).toHaveText(/Page 1 of 1/);
    await expect(page.getByTestId("result-row")).toHaveCount(N);
    await expect(page.getByTestId("page-next")).toBeDisabled();
    await expect(page.getByTestId("page-prev")).toBeDisabled();
  });
});

test("894-row result: only one page is rendered, pagination walks all rows", async ({ page }) => {
  const total = MANY_ROWS.response.meta.n_sites;
  expect(total).toBe(MANY_ROWS.response.results.length);

  await page.goto("/");
  await fillSequence(page, MANY_ROWS.request.sequence);
  await expect(page.getByTestId("sequence-length")).toContainText(
    `${MANY_ROWS.request.sequence.length} nt`,
  );
  await runAndWait(page, 15_000);

  await expect(page.getByTestId("n-sites")).toHaveText(String(total));
  await expect(page.getByTestId("visible-count")).toHaveText(`Showing ${total} of ${total} sites`);

  // Default page size 50: only the first page is in the DOM.
  await expect(page.getByTestId("page-size")).toHaveValue("50");
  await expect(page.getByTestId("page-info")).toHaveText(new RegExp(`Page 1 of ${Math.ceil(total / 50)}`));
  await expect(page.getByTestId("result-row")).toHaveCount(50);

  await selectPageSize(page, 25);
  const pages25 = Math.ceil(total / 25); // 36
  await expect(page.getByTestId("page-info")).toHaveText(new RegExp(`Page 1 of ${pages25}`));
  await expect(page.getByTestId("result-row")).toHaveCount(25);
  await expect(page.getByTestId("page-prev")).toBeDisabled();
  await page.getByTestId("page-next").click();
  await expect(page.getByTestId("page-info")).toHaveText(new RegExp(`Page 2 of ${pages25}`));
  await expect(page.getByTestId("page-prev")).toBeEnabled();
  await page.getByTestId("page-prev").click();
  await expect(page.getByTestId("page-info")).toHaveText(new RegExp(`Page 1 of ${pages25}`));

  // Walking every page (at 250/page) yields each fixture row exactly once.
  const rows = await collectAllRows(page);
  expect(rows).toHaveLength(total);
  expect(new Set(rows.map((r) => r.key))).toEqual(new Set(MANY_ROWS.response.results.map(keyOf)));
});

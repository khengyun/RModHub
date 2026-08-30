/**
 * Shared helpers for the Playwright suite.
 *
 * Expected numbers (site counts, p-values, filter results) are computed from the fixture
 * files in src/api/fixtures, which are verbatim responses of the real backend, so a test
 * never hard-codes a value that could drift from the model's ground truth.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type Locator, type Page } from "@playwright/test";
import type { ModSite, PredictRequest, PredictResponse, SampleResponse } from "../src/api/types";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const FIXTURES_DIR = fileURLToPath(new URL("../src/api/fixtures/", import.meta.url));

export function readFixture<T>(name: string): T {
  return JSON.parse(readFileSync(join(FIXTURES_DIR, name), "utf8")) as T;
}

/** 151-nt sample at alpha 0.05 with attention windows: 22 sites. */
export const GOLDEN = readFixture<PredictResponse>("golden_attention.json");
/** First 51 nt of the sample: scored, zero sites (the "empty" state). */
export const GOLDEN_51NT = readFixture<PredictResponse>("golden_51nt.json");
/** GET /api/samples/sequence. */
export const SAMPLE = readFixture<SampleResponse>("sample.json");
/** 400-nt poly-U-rich input that yields 894 sites (pagination stress). */
export const MANY_ROWS = readFixture<{ request: PredictRequest; response: PredictResponse }>(
  "many_rows.json",
);

/** The 12 modification types, in the backend's canonical order (filter chip ids). */
export const MOD_TYPES = [
  "Am", "Cm", "Gm", "Um", "m1A", "m5C", "m5U", "m6A", "m6Am", "m7G", "Psi", "AtoI",
] as const;

/** Same identity the UI uses for rows and glyphs: "{position}:{mod_type}". */
export function keyOf(site: Pick<ModSite, "position" | "mod_type">): string {
  return `${site.position}:${site.mod_type}`;
}

/** p-value as the table renders it (4 decimals). */
export function pText(site: ModSite): string {
  expect(site.p_value, `fixture row ${keyOf(site)} has a p-value`).not.toBeNull();
  return (site.p_value as number).toFixed(4);
}

export function fixtureSite(key: string): ModSite {
  const site = GOLDEN.results.find((s) => keyOf(s) === key);
  if (!site) throw new Error(`fixture golden_attention.json has no row ${key}`);
  return site;
}

// ---------------------------------------------------------------------------
// Locators
// ---------------------------------------------------------------------------

export function row(page: Page, key: string): Locator {
  return page.locator(`[data-testid="result-row"][data-key="${key}"]`);
}

export function glyph(page: Page, key: string): Locator {
  return page.locator(`[data-testid="track-site"][data-key="${key}"]`);
}

export type SortColumn = "position" | "mod_type" | "probability" | "p_value";

/** The element carrying `aria-sort` for a column (the header cell around the sort button). */
export function sortHeader(page: Page, column: SortColumn): Locator {
  return page.getByTestId(`sort-${column}`).locator("xpath=ancestor-or-self::*[@aria-sort][1]");
}

// ---------------------------------------------------------------------------
// Form actions
// ---------------------------------------------------------------------------

export async function fillSequence(page: Page, sequence: string): Promise<void> {
  await page.getByTestId("sequence-input").fill(sequence);
}

/** Press "Load sample data" and wait until the 151-nt sample is in the textarea. */
export async function loadSample(page: Page): Promise<void> {
  await page.getByTestId("load-sample").click();
  await expect(page.getByTestId("sequence-input")).toHaveValue(SAMPLE.sequence);
  await expect(page.getByTestId("sequence-length")).toContainText(`${SAMPLE.length} nt`);
}

/** Press Run and wait for the results container (success state, incl. empty results). */
export async function runAndWait(page: Page, timeout = 15_000): Promise<void> {
  const run = page.getByTestId("run");
  await expect(run).toBeEnabled();
  await run.click();
  await expect(page.getByTestId("results")).toBeVisible({ timeout });
}

/** Home -> Load sample -> Run -> results visible. */
export async function loadSampleAndRun(page: Page): Promise<void> {
  await page.goto("/");
  await loadSample(page);
  await runAndWait(page);
}

const SAW_LOADING_FLAG = "__rmodhubSawLoading";

/**
 * The sample request finishes in well under a second, so asserting the spinner with a
 * polling `toBeVisible` is racy. Instead, record from inside the page whether a
 * `[data-testid="loading"]` element was ever inserted, then read the flag afterwards.
 */
export async function armLoadingObserver(page: Page): Promise<void> {
  await page.evaluate((flag) => {
    const w = window as unknown as Record<string, unknown>;
    w[flag] = false;
    const sel = '[data-testid="loading"]';
    const mark = () => {
      w[flag] = true;
    };
    if (document.querySelector(sel)) mark();
    const obs = new MutationObserver((records) => {
      for (const r of records) {
        for (const node of Array.from(r.addedNodes)) {
          if (node instanceof Element && (node.matches(sel) || node.querySelector(sel))) mark();
        }
      }
      if (document.querySelector(sel)) mark();
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }, SAW_LOADING_FLAG);
}

export async function sawLoading(page: Page): Promise<boolean> {
  return page.evaluate(
    (flag) => (window as unknown as Record<string, unknown>)[flag] === true,
    SAW_LOADING_FLAG,
  );
}

// ---------------------------------------------------------------------------
// Results table
// ---------------------------------------------------------------------------

export interface RowInfo {
  key: string;
  position: number;
  modType: string;
  selected: boolean;
  /** Whitespace-normalised visible text of the row. */
  text: string;
}

/** The rows currently rendered (the current page only). */
export async function readRows(page: Page): Promise<RowInfo[]> {
  return page.getByTestId("result-row").evaluateAll((els) =>
    els.map((el) => ({
      key: el.getAttribute("data-key") ?? "",
      position: Number(el.getAttribute("data-position")),
      modType: el.getAttribute("data-mod-type") ?? "",
      selected: el.getAttribute("aria-selected") === "true",
      text: (el.textContent ?? "").replace(/\s+/g, " ").trim(),
    })),
  );
}

export async function selectPageSize(page: Page, size: 25 | 50 | 100 | 250): Promise<void> {
  const select = page.getByTestId("page-size");
  await select.selectOption(String(size));
  await expect(select).toHaveValue(String(size));
}

/**
 * Every row passing the current filters, across all pages: switch to the largest page
 * size, then walk `page-next` until it is disabled.
 */
export async function collectAllRows(page: Page): Promise<RowInfo[]> {
  await expect(page.getByTestId("results-table")).toBeVisible();
  if ((await page.getByTestId("page-size").count()) > 0) await selectPageSize(page, 250);
  const rows: RowInfo[] = [];
  const next = page.getByTestId("page-next");
  const info = page.getByTestId("page-info");
  for (let guard = 0; guard < 500; guard++) {
    rows.push(...(await readRows(page)));
    if ((await next.count()) === 0 || (await next.isDisabled())) break;
    const before = (await info.textContent()) ?? "";
    await next.click();
    await expect(info).not.toHaveText(before);
  }
  return rows;
}

// ---------------------------------------------------------------------------
// Small assertions
// ---------------------------------------------------------------------------

export type SortDirection = "ascending" | "descending";

export function isMonotonic<T>(values: T[], cmp: (a: T, b: T) => number, dir: SortDirection): boolean {
  for (let i = 1; i < values.length; i++) {
    const c = cmp(values[i - 1], values[i]);
    if (dir === "ascending" ? c > 0 : c < 0) return false;
  }
  return true;
}

export const numberCmp = (a: number, b: number): number => a - b;
export const asciiCmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);
export const localeCmp = (a: string, b: string): number => a.localeCompare(b, "en");

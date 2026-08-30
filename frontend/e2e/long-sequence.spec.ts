/**
 * The 10,000-nt upper bound: the request takes on the order of 10-15 s on one CPU thread.
 * The UI must show progress, stay responsive, finish within 60 s, and survive a cancel.
 */
import { expect, test } from "@playwright/test";
import { fillSequence } from "./helpers";

test("10,000 nt: loading indicator with elapsed time, results within 60 s, cancel mid-flight", async ({ page }) => {
  test.slow(); // 3x the default budget

  await page.goto("/");
  const seq = "ACGT".repeat(2500);
  expect(seq).toHaveLength(10_000);
  await fillSequence(page, seq);
  await expect(page.getByTestId("sequence-length")).toContainText("10,000 nt");
  await expect(page.getByTestId("local-error")).toHaveCount(0);
  await expect(page.getByTestId("run")).toBeEnabled();

  // --- run 1: wait for the result --------------------------------------------------
  await page.getByTestId("run").click();
  const loading = page.getByTestId("loading");
  await expect(loading).toBeVisible();
  await expect(loading).toContainText("elapsed");
  await expect(page.getByTestId("run")).toBeDisabled();
  await expect(page.getByTestId("cancel")).toBeEnabled();
  // The elapsed counter keeps ticking, i.e. the main thread is not blocked by the request.
  await expect(loading).toContainText(/[1-9]\d* s elapsed/);

  await expect(page.getByTestId("results")).toBeVisible({ timeout: 60_000 });
  await expect(loading).toHaveCount(0);
  await expect(page.getByTestId("error")).toHaveCount(0);
  await expect(page.getByTestId("summary")).toBeVisible();
  // MultiRM cannot score the first/last 25 nt: positions 26..9975.
  await expect(page.getByTestId("summary")).toContainText(/26\s*[–-]\s*9975/);
  const nSites = (await page.getByTestId("n-sites").textContent())?.trim() ?? "";
  expect(nSites).toMatch(/^\d+$/);

  // --- run 2: same input, cancelled while in flight -----------------------------------
  await expect(page.getByTestId("run")).toBeEnabled();
  await page.getByTestId("run").click();
  await expect(loading).toBeVisible();
  await page.getByTestId("cancel").click();

  await expect(loading).toHaveCount(0);
  await expect(page.getByTestId("error")).toHaveCount(0);
  // Back to the previous (successful) state, not an error and not a blank page.
  await expect(page.getByTestId("results")).toBeVisible();
  await expect(page.getByTestId("n-sites")).toHaveText(nSites);
  await expect(page.getByTestId("run")).toBeEnabled();
  await expect(page.getByTestId("clear")).toBeVisible();
});

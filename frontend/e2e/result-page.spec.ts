/**
 * /result/:jobId without a worker: GET /api/jobs/{id} and the results endpoints are
 * stubbed with page.route (fixtures from src/api/fixtures), so this runs against any
 * backend, including a sequence-only one. Covers running -> done, cancel, failed,
 * expired/unknown, the coverage warning, the read-level panel and the CSV download.
 */
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import {
  JOB_CANCELLED,
  JOB_DONE,
  JOB_FAILED,
  JOB_RUNNING,
  jsonRoute,
  SIGNAL_CSV_HEADER,
  SIGNAL_RESULTS,
  stubCapabilities,
  stubResults,
} from "./helpers";

const ID = JOB_DONE.job_id;

test.beforeEach(async ({ page }) => {
  await stubCapabilities(page, true);
});

test("running -> done: stage, progress, ETA, then summary, warning, table, drill-down, CSV", async ({ page }, testInfo) => {
  let polls = 0;
  await page.route(`**/api/jobs/${ID}`, (route) => {
    polls += 1;
    return route.fulfill(jsonRoute(polls < 3 ? JOB_RUNNING : JOB_DONE));
  });
  await stubResults(page, ID);

  await page.goto(`/result/${ID}`);
  const status = page.getByTestId("job-status");
  await expect(status).toHaveAttribute("data-status", "running");
  await expect(page.getByTestId("job-status-pill")).toHaveText(/running/i);
  await expect(page.getByTestId("job-stage")).toContainText("Extracting features");
  await expect(page.getByTestId("job-progress")).toHaveAttribute("aria-valuenow", "42");
  await expect(page.getByTestId("job-eta")).toContainText("1 min 35 s");
  await expect(page.getByTestId("job-elapsed")).toContainText("Elapsed");
  await expect(page.getByTestId("job-cancel")).toBeEnabled();
  await expect(page.getByTestId("data-lifecycle")).toBeVisible();

  // Two more polls (2 s + 3 s) and the job is done.
  await expect(status).toHaveAttribute("data-status", "done", { timeout: 15_000 });
  expect(polls).toBeGreaterThanOrEqual(3);
  await expect(page.getByTestId("job-cancel")).toBeDisabled();
  await expect(page.getByTestId("job-expires")).not.toHaveText("—");
  await expect(page.getByTestId("job-stage")).toContainText("Finished");

  await expect(page.getByTestId("n-sites")).toHaveText(String(SIGNAL_RESULTS.meta.n_sites));
  const low = SIGNAL_RESULTS.results.filter((s) => s.coverage < SIGNAL_RESULTS.meta.low_coverage_threshold).length;
  expect(low).toBeGreaterThan(0);
  await expect(page.getByTestId("coverage-warning")).toContainText(`${low} of ${SIGNAL_RESULTS.results.length} sites`);
  await expect(page.getByTestId("coverage-warning")).toContainText(/unreliable/);

  const select = page.getByTestId("transcript-select");
  await expect(select).toHaveValue("tx_A");
  const headers = page.getByTestId("results-table").getByRole("columnheader");
  await expect(headers.filter({ hasText: "Coverage" })).toHaveCount(1);
  await expect(headers.filter({ hasText: "95% CI" })).toHaveCount(1);
  await expect(headers.filter({ hasText: "p-value" })).toHaveCount(0);
  const txA = SIGNAL_RESULTS.results.filter((s) => s.transcript_id === "tx_A");
  await expect(page.getByTestId("result-row")).toHaveCount(txA.length);
  await expect(page.getByTestId("track-view")).toBeVisible();
  await expect(page.getByTestId("track-legend")).toContainText("modification rate");

  // Row click -> read-level panel with server-paged reads.
  await page.locator('[data-testid="result-row"][data-key="101:m6A:+"]').click();
  const panel = page.getByTestId("read-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId("read-row")).toHaveCount(25);
  await expect(panel).toContainText("rows 1–25 of 40");
  await panel.getByTestId("page-next").click();
  await expect(panel).toContainText("Page 2 of 2");

  // Switching the transcript re-renders the table and closes the panel.
  await select.selectOption("tx_B");
  const txB = SIGNAL_RESULTS.results.filter((s) => s.transcript_id === "tx_B");
  await expect(page.getByTestId("result-row")).toHaveCount(txB.length);
  await expect(panel).toHaveCount(0);

  // Server CSV download.
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByTestId("download-csv").click()]);
  expect(download.suggestedFilename()).toBe(`rmodhub_signal_${ID}_sites.csv`);
  const file = testInfo.outputPath("sites.csv");
  await download.saveAs(file);
  expect(readFileSync(file, "utf8").split(/\r?\n/)[0]).toBe(SIGNAL_CSV_HEADER);

  // Copy link: either the clipboard worked or the fallback text explains.
  await page.getByTestId("copy-link").click();
  await expect(page.getByTestId("copy-link")).toHaveText(/Link copied|Copy failed/);
});

test("Cancel job posts /cancel and adopts the cancelled status", async ({ page }) => {
  let cancelled = false;
  await page.route(`**/api/jobs/${ID}`, (route) => route.fulfill(jsonRoute(cancelled ? JOB_CANCELLED : JOB_RUNNING)));
  await page.route(`**/api/jobs/${ID}/cancel`, (route) => {
    expect(route.request().method()).toBe("POST");
    cancelled = true;
    return route.fulfill(jsonRoute(JOB_CANCELLED));
  });
  await page.goto(`/result/${ID}`);
  await expect(page.getByTestId("job-status")).toHaveAttribute("data-status", "running");
  await page.getByTestId("job-cancel").click();
  await expect(page.getByTestId("job-status")).toHaveAttribute("data-status", "cancelled");
  await expect(page.getByTestId("job-cancelled")).toBeVisible();
  await expect(page.getByTestId("job-cancel")).toBeDisabled();
  expect(cancelled).toBe(true);
  await expect(page.getByTestId("results")).toHaveCount(0);
});

test("failed job shows the error sentence and no results", async ({ page }) => {
  await page.route(`**/api/jobs/${ID}`, (route) => route.fulfill(jsonRoute(JOB_FAILED)));
  await page.goto(`/result/${ID}`);
  await expect(page.getByTestId("job-status")).toHaveAttribute("data-status", "failed");
  await expect(page.getByTestId("job-error")).toContainText("dorado --emit-moves");
  await expect(page.getByTestId("job-cancel")).toBeDisabled();
  await expect(page.getByTestId("results")).toHaveCount(0);
});

test("expired / unknown job (404) shows the missing-job notice and stops polling", async ({ page }) => {
  let polls = 0;
  await page.route(`**/api/jobs/${ID}`, (route) => {
    polls += 1;
    return route.fulfill(jsonRoute({ detail: "Unknown or expired job." }, 404));
  });
  await page.goto(`/result/${ID}`);
  const missing = page.getByTestId("job-missing");
  await expect(missing).toBeVisible();
  await expect(missing).toContainText(/unknown or expired/i);
  await expect(missing).toContainText("14 days");
  // React StrictMode (dev builds) mounts effects twice, so the very first poll may be
  // issued and aborted once more; what matters is that no further poll follows the 404.
  const seen = polls;
  expect(seen).toBeLessThanOrEqual(2);
  await page.waitForTimeout(4_500);
  expect(polls).toBe(seen);
  await expect(page.getByTestId("job-status")).toHaveCount(0);
});

test("a malformed job id never hits the API", async ({ page }) => {
  let hits = 0;
  await page.route("**/api/jobs/**", (route) => {
    hits += 1;
    return route.fulfill(jsonRoute({ detail: "no" }, 404));
  });
  await page.goto("/result/not-a-job");
  await expect(page.getByTestId("job-missing")).toContainText(/not a valid job link/);
  expect(hits).toBe(0);
});

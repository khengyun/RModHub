/**
 * The signal branch end to end on a REAL stack (API + worker + queue): Load sample data ->
 * /result/<id> -> stages -> results with the Coverage column, coverage warning, read-level
 * drill-down and the CSV download. Skipped unless GET /api/capabilities reports
 * signal: true (a sequence-only deployment).
 */
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { serverCapabilities, SIGNAL_CSV_HEADER } from "./helpers";

const JOB_BUDGET_MS = 14 * 60_000;

const TERMINAL = ["done", "failed", "cancelled", "expired"];

test.describe("signal branch (real stack)", () => {
  let jobId: string | null = null;

  // Leave no queued/running job behind: max_running_per_ip is 1, so a leftover job would
  // turn the next run's "Load sample" into a 429.
  test.afterEach(async ({ request }) => {
    if (!jobId) return;
    const res = await request.get(`/api/jobs/${jobId}`);
    if (res.ok()) {
      const job = (await res.json()) as { status: string };
      if (!TERMINAL.includes(job.status)) await request.post(`/api/jobs/${jobId}/cancel`);
    }
    jobId = null;
  });

  test("Load sample -> job page -> stages -> results", async ({ page, request }, testInfo) => {
    const caps = await serverCapabilities(request);
    test.skip(!caps?.signal, "the nanopore signal branch is not enabled on this server");
    test.setTimeout(JOB_BUDGET_MS + 60_000);

    await page.goto("/");
    await page.getByTestId("nav-signal").click();
    await expect(page.getByTestId("signal-page")).toBeVisible();
    await expect(page.getByTestId("data-lifecycle")).toContainText(`${caps!.retention.results_days} days`);

    await page.getByTestId("load-sample").click();
    // A job from an earlier run (or a manual click) may still hold the per-address quota.
    const outcome = await Promise.race([
      page.waitForURL(/\/result\/[0-9a-f-]{36}$/, { timeout: 30_000 }).then(() => "started" as const, () => "timeout" as const),
      page.getByTestId("error").waitFor({ state: "visible", timeout: 30_000 }).then(() => "error" as const, () => "timeout" as const),
    ]);
    if (outcome !== "started") {
      const text = ((await page.getByTestId("error").textContent().catch(() => null)) ?? "").trim();
      test.skip(/already have/i.test(text), `per-address quota in use: ${text}`);
      throw new Error(`Load sample did not reach the result page (${outcome}): ${text}`);
    }
    jobId = page.url().split("/result/")[1];

    const status = page.getByTestId("job-status");
    await expect(status).toBeVisible();
    await expect(status).toHaveAttribute("data-status", /queued|running|done/);
    await expect(page.getByTestId("job-cancel")).toBeAttached();
    await expect(page.getByTestId("copy-link")).toBeVisible();

    // The stage line changes as the worker progresses (at least one non-empty stage label).
    await expect
      .poll(async () => (await page.getByTestId("job-stage").textContent()) ?? "", { timeout: JOB_BUDGET_MS })
      .toMatch(/Stage: (Preparing|Sampling reads|Extracting features|De novo screen|Inference|Aggregating|Waiting|Finished)/);
    await expect(status).toHaveAttribute("data-status", "done", { timeout: JOB_BUDGET_MS });
    await expect(page.getByTestId("job-error")).toHaveCount(0);

    const summary = page.getByTestId("signal-summary");
    await expect(summary).toBeVisible();
    await expect(summary).toContainText("DirectRM");
    const nSites = Number(((await page.getByTestId("n-sites").textContent()) ?? "0").replace(/,/g, ""));
    if (nSites === 0) {
      await expect(page.getByTestId("empty")).toBeVisible();
      testInfo.annotations.push({ type: "note", description: "sample produced 0 sites; table assertions skipped" });
      return;
    }

    // Table with the Coverage column; the low-coverage warning must be consistent with the rows.
    const table = page.getByTestId("results-table");
    await expect(table).toBeVisible();
    const headers = (await table.getByRole("columnheader").allTextContents()).map((t) => t.trim());
    expect(headers).toContain("Coverage");
    expect(headers).not.toContain("p-value");
    const covIdx = headers.indexOf("Coverage");
    const rows = page.getByTestId("result-row");
    const rowCount = await rows.count();
    let lowInTable = 0;
    for (let i = 0; i < rowCount; i++) {
      const cov = Number((await rows.nth(i).getByRole("cell").nth(covIdx).textContent())?.trim());
      if (cov < 30) lowInTable += 1;
    }
    const warning = page.getByTestId("coverage-warning");
    if (lowInTable > 0) await expect(warning).toBeVisible();
    if ((await warning.count()) === 0) expect(lowInTable).toBe(0);

    // Row click opens the read-level panel.
    await rows.first().click();
    const panel = page.getByTestId("read-panel");
    await expect(panel).toBeVisible();
    await expect(panel.getByTestId("read-row").first()).toBeVisible({ timeout: 30_000 });

    // Server CSV header.
    const [download] = await Promise.all([page.waitForEvent("download"), page.getByTestId("download-csv").click()]);
    expect(download.suggestedFilename()).toBe(`rmodhub_signal_${jobId}_sites.csv`);
    const file = testInfo.outputPath("signal_sites.csv");
    await download.saveAs(file);
    const lines = readFileSync(file, "utf8").split(/\r?\n/).filter((l) => l.length > 0);
    expect(lines[0]).toBe(SIGNAL_CSV_HEADER);
    expect(lines.length - 1).toBe(nSites);
    expect(lines.slice(1).every((l) => l.split(",")[6] === "signal")).toBe(true);
  });
});

/**
 * Navigation and the secondary pages: Help (how to read results), the Nanopore signal tab
 * (present only when GET /api/capabilities reports signal: true), the /result route, the
 * self-hosted Swagger UI, and the license notice on every page.
 */
import { expect, test } from "@playwright/test";
import { serverCapabilities, UNKNOWN_JOB_ID } from "./helpers";

const HELP_ANCHORS = [
  "reading-results", "flanks", "mod-types", "multiple-mods", "citation",
  "nanopore-signal", "signal-files", "signal-regions", "signal-coverage", "signal-jobs", "signal-data",
];
const PAGES = ["/", "/help", "/signal", `/result/${UNKNOWN_JOB_ID}`];

test("Help page explains how to read the results of both branches", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("nav-help").click();
  await expect(page).toHaveURL(/\/help\/?$/);
  const help = page.getByTestId("help-page");
  await expect(help).toBeVisible();
  for (const id of HELP_ANCHORS) {
    await expect(page.locator(`#${id}`), `anchor #${id}`).toBeAttached();
  }
  await expect(help).toContainText(/p-value/i);
  await expect(help).toContainText("25 nt");
  await expect(help).toContainText("MultiRM");
  await expect(help).toContainText("DirectRM");
  await expect(help).toContainText("--emit-moves");
  await expect(help).not.toContainText(/phase 2/i);
});

test("Help anchors are reachable by URL fragment", async ({ page }) => {
  await page.goto("/help#flanks");
  await expect(page.getByTestId("help-page")).toBeVisible();
  await expect(page.locator("#flanks")).toBeInViewport();
  await page.goto("/help#signal-regions");
  await expect(page.locator("#signal-regions")).toBeInViewport();
});

test("the Nanopore signal tab follows GET /api/capabilities", async ({ page, request }) => {
  const caps = await serverCapabilities(request);
  await page.goto("/");
  if (caps?.signal) {
    await page.getByTestId("nav-signal").click();
    await expect(page).toHaveURL(/\/signal\/?$/);
    const signalPage = page.getByTestId("signal-page");
    await expect(signalPage).toBeVisible();
    for (const slot of ["pod5", "bam", "reference", "regions"]) {
      await expect(page.getByTestId(`upload-${slot}`)).toBeAttached();
    }
    await expect(page.getByTestId("load-sample")).toBeVisible();
    await expect(page.getByTestId("data-lifecycle")).toBeVisible();
    await expect(page.getByTestId("run")).toBeDisabled();
  } else {
    await expect(page.getByTestId("nav-signal")).toHaveCount(0);
    await page.goto("/signal");
    await expect(page.getByTestId("signal-disabled")).toContainText("not enabled on this server");
    await expect(page.getByTestId("signal-page")).toHaveCount(0);
  }
  // Either way: no upload form of the sequence tool, and no placeholder wording.
  await expect(page.getByTestId("sequence-input")).toHaveCount(0);
  await expect(page.locator("main")).not.toContainText(/phase 2|coming soon/i);
  await expect(page.getByTestId("health-indicator")).toHaveCount(0);
});

test("Sequence tab returns to the landing tool", async ({ page }) => {
  await page.goto("/help");
  await page.getByTestId("nav-sequence").click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("sequence-input")).toBeVisible();
});

test("deep links to SPA routes are served (history fallback)", async ({ page, request }) => {
  const caps = await serverCapabilities(request);
  await page.goto("/help");
  await expect(page.getByTestId("help-page")).toBeVisible();
  await page.goto("/signal");
  await expect(page.getByTestId("signal-page").or(page.getByTestId("signal-disabled"))).toBeVisible();
  // A bookmarked result page for a job the server does not know: "unknown" when the signal
  // branch answers, the disabled notice (no endless polling) on a sequence-only stack.
  await page.goto(`/result/${UNKNOWN_JOB_ID}`);
  if (caps?.signal) {
    await expect(page.getByTestId("job-missing")).toBeVisible();
    await expect(page.getByTestId("job-missing")).toContainText(/unknown or expired/i);
  } else {
    await expect(page.getByTestId("signal-disabled")).toContainText("not enabled on this server");
    await expect(page.getByTestId("error")).toHaveCount(0);
  }
  // Unknown routes fall back to the sequence tool.
  await page.goto("/does-not-exist");
  await expect(page.getByTestId("sequence-input")).toBeVisible();
});

test("/docs is the self-hosted Swagger UI (no CDN)", async ({ request }) => {
  const res = await request.get("/docs");
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toContain("text/html");
  const html = await res.text();
  expect(html).toContain("/static/swagger/");
  expect(html).not.toMatch(/https?:\/\/(cdn\.|unpkg\.|cdnjs\.)/);

  const spec = await request.get("/openapi.json");
  expect(spec.status()).toBe(200);
  const json = (await spec.json()) as { paths?: Record<string, unknown> };
  expect(Object.keys(json.paths ?? {})).toContain("/api/predict/sequence");
});

for (const path of PAGES) {
  test(`license notice is on ${path}`, async ({ page }) => {
    await page.goto(path);
    const footer = page.getByTestId("footer-license");
    await expect(footer).toBeVisible();
    await expect(footer).toContainText("MIT");
    await expect(footer).toContainText("MultiRM");
    await expect(footer).toContainText("DirectRM");
  });
}

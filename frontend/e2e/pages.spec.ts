/**
 * Navigation and the secondary pages: Help (how to read results), the phase-2 signal
 * placeholder, the self-hosted Swagger UI, and the license notice on every page.
 */
import { expect, test } from "@playwright/test";

const HELP_ANCHORS = ["reading-results", "flanks", "mod-types", "multiple-mods", "citation"];
const PAGES = ["/", "/help", "/signal"];

test("Help page explains how to read the results", async ({ page }) => {
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
});

test("Help anchors are reachable by URL fragment", async ({ page }) => {
  await page.goto("/help#flanks");
  await expect(page.getByTestId("help-page")).toBeVisible();
  await expect(page.locator("#flanks")).toBeInViewport();
});

test("Signal tab is a phase-2 placeholder", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("nav-signal").click();
  await expect(page).toHaveURL(/\/signal\/?$/);
  await expect(page.locator("main")).toContainText(/phase 2/i);
  // No upload form yet: the sequence tool is not rendered here.
  await expect(page.getByTestId("sequence-input")).toHaveCount(0);
});

test("Sequence tab returns to the landing tool", async ({ page }) => {
  await page.goto("/help");
  await page.getByTestId("nav-sequence").click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("sequence-input")).toBeVisible();
});

test("deep links to SPA routes are served (history fallback)", async ({ page }) => {
  await page.goto("/help");
  await expect(page.getByTestId("help-page")).toBeVisible();
  await page.goto("/signal");
  await expect(page.locator("main")).toContainText(/phase 2/i);
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
  });
}

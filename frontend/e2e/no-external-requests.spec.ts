/**
 * NAR Web Server Issue proof: the application never contacts a third party and never
 * sets a cookie. Every browser request during a full session is recorded and must stay
 * on the app's own origin; no response may carry Set-Cookie; the cookie jar stays empty.
 */
import { expect, test, type Page } from "@playwright/test";
import { loadSample, row, runAndWait } from "./helpers";

interface NetworkLog {
  offOrigin: string[];
  cookieSetters: string[];
  settle: () => Promise<void>;
}

/** Subscribe BEFORE the first navigation so the document request itself is covered. */
function recordNetwork(page: Page, origin: string): NetworkLog {
  const offOrigin: string[] = [];
  const cookieSetters: string[] = [];
  const pending: Promise<void>[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (/^(data|blob|about):/i.test(url)) return; // in-memory, never leaves the page
    if (new URL(url).origin !== origin) offOrigin.push(url);
  });
  page.on("response", (res) => {
    pending.push(
      res
        .allHeaders()
        .then((headers) => {
          if ("set-cookie" in headers) cookieSetters.push(`${res.url()} -> ${headers["set-cookie"]}`);
        })
        .catch(() => {
          /* response body already gone (navigation); nothing to record */
        }),
    );
  });
  return {
    offOrigin,
    cookieSetters,
    settle: async () => {
      await Promise.all(pending);
    },
  };
}

test.describe("NAR compliance: same-origin only, no cookies", () => {
  test("a full session (sample run, selection, CSV, Help, Signal) stays on the app origin", async ({
    page,
    context,
    baseURL,
  }) => {
    const origin = new URL(baseURL as string).origin;
    const log = recordNetwork(page, origin);

    await page.goto("/");
    await loadSample(page);
    await runAndWait(page);
    await row(page, "52:Gm").click();
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByTestId("download-csv").click(),
    ]);
    await download.path();

    await page.getByTestId("nav-help").click();
    await expect(page.getByTestId("help-page")).toBeVisible();
    await page.getByTestId("nav-signal").click();
    await expect(page.locator("main")).toContainText(/phase 2/i);
    await page.getByTestId("nav-sequence").click();
    await expect(page.getByTestId("sequence-input")).toBeVisible();
    await page.waitForLoadState("networkidle");
    await log.settle();

    expect(log.offOrigin, "requests leaving the app origin").toEqual([]);
    expect(log.cookieSetters, "responses with Set-Cookie").toEqual([]);
    expect(await page.evaluate(() => document.cookie)).toBe("");
    expect(await context.cookies()).toEqual([]);
  });

  test("/docs (Swagger UI) loads only same-origin assets", async ({ page, context, baseURL }) => {
    const origin = new URL(baseURL as string).origin;
    const log = recordNetwork(page, origin);

    await page.goto("/docs");
    // Swagger UI has rendered the spec: the API title and at least one operation.
    await expect(page.locator(".swagger-ui")).toBeVisible();
    await expect(page.locator(".swagger-ui .opblock").first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    await log.settle();

    expect(log.offOrigin, "requests leaving the app origin").toEqual([]);
    expect(log.cookieSetters, "responses with Set-Cookie").toEqual([]);
    expect(await context.cookies()).toEqual([]);
  });
});

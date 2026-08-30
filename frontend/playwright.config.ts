/**
 * Playwright configuration for the RModHub end-to-end suite (frontend/e2e).
 *
 * Targets:
 *   - default: the Docker stack (`docker compose up -d --build --wait` from the repo root,
 *     nginx `web` service on http://localhost:8080 proxying the API);
 *   - E2E_BASE_URL=<url>: any other running deployment;
 *   - E2E_START_VITE=1: local development. Playwright starts `vite --port 5173` itself
 *     (proxying API paths to VITE_API_TARGET, default http://localhost:8000, so run
 *     `uv run uvicorn app.main:app --port 8000` first) and targets http://localhost:5173.
 *
 * Chromium only: the browsers are installed with `npx playwright install chromium`.
 */
import { defineConfig, devices } from "@playwright/test";

const startVite = process.env.E2E_START_VITE === "1";
const baseURL =
  process.env.E2E_BASE_URL ?? (startVite ? "http://localhost:5173" : "http://localhost:8080");

export default defineConfig({
  testDir: "e2e",
  // Per-test budget. The 10,000-nt test calls `test.slow()` (x3 = 90 s).
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // The model server scores with a single torch thread; keep contention low so the
  // timing assertions (results within N seconds) stay meaningful.
  workers: process.env.CI ? 1 : 2,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL,
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
    acceptDownloads: true,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
  ],
  webServer: startVite
    ? {
        command: "npx vite --port 5173 --strictPort",
        url: "http://localhost:5173",
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
        env: {
          VITE_API_TARGET: process.env.VITE_API_TARGET ?? "http://localhost:8000",
        },
      }
    : undefined,
});

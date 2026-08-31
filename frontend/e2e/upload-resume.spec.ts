/**
 * The resumable upload against an in-memory tus server implemented with page.route:
 * POST /api/jobs/signal/init, HEAD/PATCH /api/uploads/<id>, POST /api/jobs/<id>/start.
 * No real backend is needed. The first PATCH of the pod5 slot is aborted at the network
 * level; the client must recover via HEAD and finish all four slots, then navigate to
 * /result/<id>. A second test reloads mid-upload and resumes from localStorage.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { JOB_QUEUED, JOB_UPLOADING, jsonRoute, SAMPLES_SIGNAL, stubCapabilities } from "./helpers";

const ID = JOB_UPLOADING.job_id;
const SLOTS = ["pod5", "bam", "reference", "regions"] as const;
type Slot = (typeof SLOTS)[number];
const SIZES: Record<Slot, number> = { pod5: 300_000, bam: 40_000, reference: 2_000, regions: 80 };
const NAMES: Record<Slot, string> = { pod5: "run.pod5", bam: "run.bam", reference: "ref.fa", regions: "regions.csv" };

interface TusServer {
  offsets: Map<string, number>;
  lengths: Map<string, number>;
  patches: string[];
  /** Slots whose next PATCH is aborted at the network level (once). */
  abortNext: Set<Slot>;
  /** Slots whose PATCH is never answered (to simulate a stalled connection). */
  hold: Set<Slot>;
  started: number;
}

async function installTusServer(page: Page): Promise<TusServer> {
  const s: TusServer = { offsets: new Map(), lengths: new Map(), patches: [], abortNext: new Set(), hold: new Set(), started: 0 };
  const urlOf = (slot: Slot) => `/api/uploads/u-${slot}`;
  const slotOf = (url: string) => SLOTS.find((x) => url.endsWith(urlOf(x)))!;

  await page.route("**/api/samples/signal", (route) => route.fulfill(jsonRoute(SAMPLES_SIGNAL)));
  await page.route("**/api/jobs/signal/init", (route) => {
    const body = route.request().postDataJSON() as { kit: string; files: Record<Slot, { name: string; size: number }> };
    expect(Object.keys(body.files).sort()).toEqual([...SLOTS].sort());
    const uploads = Object.fromEntries(
      SLOTS.map((slot) => {
        s.lengths.set(urlOf(slot), body.files[slot].size);
        if (!s.offsets.has(urlOf(slot))) s.offsets.set(urlOf(slot), 0);
        return [slot, { url: urlOf(slot), length: body.files[slot].size, offset: s.offsets.get(urlOf(slot)), complete: false }];
      }),
    );
    return route.fulfill(jsonRoute({ ...JOB_UPLOADING, kit: body.kit, uploads }, 201));
  });
  await page.route("**/api/uploads/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url()).pathname;
    const slot = slotOf(url);
    const cur = s.offsets.get(url) ?? 0;
    if (req.method() === "HEAD") {
      return route.fulfill({
        status: 200,
        headers: { "Tus-Resumable": "1.0.0", "Upload-Offset": String(cur), "Upload-Length": String(s.lengths.get(url) ?? 0), "Cache-Control": "no-store" },
      });
    }
    if (req.method() === "PATCH") {
      if (s.hold.has(slot)) return; // never answered
      if (s.abortNext.has(slot)) {
        s.abortNext.delete(slot);
        return route.abort("failed");
      }
      expect(req.headers()["tus-resumable"]).toBe("1.0.0");
      expect(req.headers()["content-type"]).toBe("application/offset+octet-stream");
      const offset = Number(req.headers()["upload-offset"]);
      if (offset !== cur) return route.fulfill({ status: 409, headers: { "Tus-Resumable": "1.0.0" } });
      // Chromium hands us the body; if it ever does not, fall back to "the rest of the file".
      const body = req.postDataBuffer();
      const len = body ? body.length : Math.min(16 * 1024 * 1024, (s.lengths.get(url) ?? 0) - cur);
      s.offsets.set(url, cur + len);
      s.patches.push(`${slot}@${offset}+${len}`);
      return route.fulfill({ status: 204, headers: { "Tus-Resumable": "1.0.0", "Upload-Offset": String(cur + len) } });
    }
    return route.fulfill({ status: 405 });
  });
  await page.route(`**/api/jobs/${ID}/start`, async (route) => {
    // Every slot must be complete on the server, and the page must show it.
    for (const slot of SLOTS) {
      expect(s.offsets.get(urlOf(slot)), `${slot} complete on the server`).toBe(SIZES[slot]);
      await expect(page.getByTestId(`upload-status-${slot}`)).toHaveText("done");
      await expect(page.getByTestId(`upload-progress-${slot}`)).toHaveAttribute("aria-valuenow", "100");
    }
    s.started += 1;
    return route.fulfill(jsonRoute(JOB_QUEUED, 202));
  });
  await page.route(`**/api/jobs/${ID}`, (route) => {
    const uploads = Object.fromEntries(
      SLOTS.map((slot) => {
        const len = s.lengths.get(urlOf(slot)) ?? SIZES[slot];
        const off = s.offsets.get(urlOf(slot)) ?? 0;
        return [slot, { url: urlOf(slot), length: len, offset: off, complete: off >= len }];
      }),
    );
    return route.fulfill(jsonRoute(s.started > 0 ? JOB_QUEUED : { ...JOB_UPLOADING, uploads }));
  });
  return s;
}

/** Real files on disk so that lastModified (part of the resume fingerprint) is stable across picks. */
function writeInputFiles(dir: string): Record<Slot, string> {
  mkdirSync(dir, { recursive: true });
  const out = {} as Record<Slot, string>;
  for (const slot of SLOTS) {
    out[slot] = join(dir, NAMES[slot]);
    writeFileSync(out[slot], Buffer.alloc(SIZES[slot], slot.charCodeAt(0)));
  }
  return out;
}

async function pickFiles(page: Page, files: Record<Slot, string>): Promise<void> {
  for (const slot of SLOTS) await page.getByTestId(`upload-${slot}`).setInputFiles(files[slot]);
  for (const slot of SLOTS) await expect(page.getByTestId(`upload-name-${slot}`)).toContainText(NAMES[slot]);
}

test.beforeEach(async ({ page }) => {
  await stubCapabilities(page, true);
});

test("four slots upload through tus; an aborted PATCH is recovered via HEAD; the page reaches /result", async ({ page }, testInfo) => {
  const server = await installTusServer(page);
  server.abortNext.add("pod5");
  const files = writeInputFiles(testInfo.outputPath("inputs"));

  await page.goto("/signal");
  await expect(page.getByTestId("run")).toBeDisabled();
  await pickFiles(page, files);
  await expect(page.getByTestId("local-error")).toHaveCount(0);
  await expect(page.getByTestId("run")).toBeEnabled();
  await page.getByTestId("kit-RNA002").check();
  await page.getByTestId("run").click();

  await expect(page).toHaveURL(new RegExp(`/result/${ID}$`), { timeout: 20_000 });
  await expect(page.getByTestId("job-status")).toHaveAttribute("data-status", "queued");

  expect(server.started).toBe(1);
  expect(server.abortNext.size).toBe(0); // the abort was consumed ...
  expect(server.patches.filter((p) => p.startsWith("pod5@0+"))).toHaveLength(1); // ... and the chunk re-sent after HEAD
  for (const slot of SLOTS) expect(server.offsets.get(`/api/uploads/u-${slot}`)).toBe(SIZES[slot]);
  // The resume record is gone once the job started.
  expect(await page.evaluate(() => window.localStorage.getItem("rmodhub.signal.resume.v1"))).toBeNull();
});

test("a reload mid-upload can be resumed by picking the same four files again", async ({ page }, testInfo) => {
  test.slow();
  const server = await installTusServer(page);
  server.hold.add("pod5"); // the pod5 PATCH stalls forever
  const files = writeInputFiles(testInfo.outputPath("inputs"));

  await page.goto("/signal");
  await pickFiles(page, files);
  await page.getByTestId("run").click();
  await expect(page.getByTestId("upload-overall")).toBeVisible();
  // The three small slots complete while the pod5 is stuck.
  for (const slot of ["bam", "reference", "regions"] as const) {
    await expect(page.getByTestId(`upload-status-${slot}`)).toHaveText("done", { timeout: 15_000 });
  }
  await expect(page.getByTestId("upload-status-pod5")).toContainText("uploading");
  expect(await page.evaluate(() => window.localStorage.getItem("rmodhub.signal.resume.v1"))).not.toBeNull();

  // Reload: the in-flight PATCH dies with the page; the server keeps the three finished slots.
  server.hold.delete("pod5");
  await page.reload();
  await expect(page.getByTestId("signal-page")).toBeVisible();
  await expect(page.getByTestId("resume-prompt")).toHaveCount(0);
  await pickFiles(page, files);
  const prompt = page.getByTestId("resume-prompt");
  await expect(prompt).toBeVisible();
  await expect(prompt).toContainText(/Resume it where it stopped/);
  await page.getByTestId("resume-yes").click();

  await expect(page).toHaveURL(new RegExp(`/result/${ID}$`), { timeout: 20_000 });
  expect(server.started).toBe(1);
  // Only the pod5 was (re)sent after the resume; the other three were already complete.
  const afterResume = server.patches.filter((p) => !p.startsWith("pod5@"));
  expect(afterResume).toHaveLength(3);
  for (const slot of SLOTS) expect(server.offsets.get(`/api/uploads/u-${slot}`)).toBe(SIZES[slot]);
});

test("client-side validation: pod5 alone asks for the BAM; wrong extensions are named; nothing is sent", async ({ page }, testInfo) => {
  let inits = 0;
  await page.route("**/api/jobs/signal/init", (route) => {
    inits += 1;
    return route.fulfill(jsonRoute({ detail: "should not be called" }, 500));
  });
  await page.route("**/api/samples/signal", (route) => route.fulfill(jsonRoute(SAMPLES_SIGNAL)));
  const files = writeInputFiles(testInfo.outputPath("inputs"));
  await page.goto("/signal");

  await page.getByTestId("upload-pod5").setInputFiles(files.pod5);
  await expect(page.getByTestId("upload-error-bam")).toContainText("A BAM file is required");
  await expect(page.getByTestId("local-error")).toContainText("pod5 file alone cannot be analysed");
  await expect(page.getByTestId("run")).toBeDisabled();

  const wrong = join(testInfo.outputPath("inputs"), "reads.sam");
  writeFileSync(wrong, "x");
  await page.getByTestId("upload-bam").setInputFiles(wrong);
  await expect(page.getByTestId("upload-error-bam")).toContainText('Expected a .bam file, got "reads.sam".');
  await page.getByTestId("upload-bam").setInputFiles(files.bam);
  await page.getByTestId("upload-reference").setInputFiles(files.reference);
  await page.getByTestId("upload-regions").setInputFiles(files.regions);
  await expect(page.getByTestId("local-error")).toHaveCount(0);
  await expect(page.getByTestId("run")).toBeEnabled();
  expect(inits).toBe(0);
});

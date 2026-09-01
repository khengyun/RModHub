import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMemoryRouter, Link, RouterProvider, useParams } from "react-router-dom";
import capabilities from "../api/fixtures/capabilities.json";
import jobCancelled from "../api/fixtures/job_cancelled.json";
import jobQueued from "../api/fixtures/job_queued.json";
import jobUploading from "../api/fixtures/job_uploading.json";
import samples from "../api/fixtures/samples_signal.json";
import { fileFingerprint } from "../api/tus";
import { UPLOAD_SLOTS, type Capabilities, type UploadSlot } from "../api/types";
import {
  CapabilitiesProvider,
  DEFAULT_CAPABILITIES,
  type CapabilitiesState,
} from "../components/layout/CapabilitiesProvider";
import { RESUME_STORAGE_KEY } from "../components/signal/resumeStore";
import { SignalPage } from "./SignalPage";

const ENABLED: CapabilitiesState = { status: "ready", capabilities: capabilities as Capabilities };
const JOB = jobUploading.job_id;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** Job in state "uploading" with fresh (offset 0) upload URLs for every slot. */
function uploadingJob(offsets: Partial<Record<UploadSlot, number>> = {}) {
  const uploads = Object.fromEntries(
    UPLOAD_SLOTS.map((s) => [s, { url: `/api/uploads/u-${s}`, length: 0, offset: offsets[s] ?? 0, complete: false }]),
  );
  return { ...jobUploading, uploads };
}

/* ---------- in-memory tus server behind a global XMLHttpRequest stub ---------- */

const tus = {
  offsets: new Map<string, number>(),
  log: [] as string[],
  /** Upload URLs whose PATCH is never answered (a stalled connection) until aborted. */
  hold: new Set<string>(),
};

class FakeXHR {
  status = 0;
  timeout = 0;
  upload: { onprogress: ((e: ProgressEvent) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  private method = "";
  private url = "";
  private headers: Record<string, string> = {};
  private res: Record<string, string> = {};
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(k: string, v: string) {
    this.headers[k] = v;
  }
  getResponseHeader(n: string) {
    return this.res[n] ?? null;
  }
  abort() {
    this.onabort?.();
  }
  send(body: Blob | null) {
    tus.log.push(`${this.method} ${this.url} ${this.headers["Upload-Offset"] ?? ""}`.trim());
    const current = tus.offsets.get(this.url) ?? 0;
    if (this.method === "PATCH" && tus.hold.has(this.url)) return; // stalled: only abort() ends it
    queueMicrotask(() => {
      if (this.method === "HEAD") {
        this.status = 200;
        this.res = { "Upload-Offset": String(current) };
      } else if (this.method === "DELETE") {
        this.status = 204;
        this.res = {};
      } else if (Number(this.headers["Upload-Offset"]) !== current) {
        this.status = 409;
      } else {
        const next = current + (body?.size ?? 0);
        tus.offsets.set(this.url, next);
        if (body) this.upload.onprogress?.({ loaded: body.size } as ProgressEvent);
        this.status = 204;
        this.res = { "Upload-Offset": String(next) };
      }
      this.onload?.();
    });
  }
}

function stubApi(overrides: Partial<Record<"init" | "start" | "sample" | "job", () => Response>> = {}) {
  const calls: { url: string; method: string; body?: string }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? init.body : undefined });
    if (url === "/api/samples/signal") return json(samples);
    if (url === "/api/jobs/signal/sample") return (overrides.sample ?? (() => json(jobQueued, 202)))();
    if (url === "/api/jobs/signal/init") return (overrides.init ?? (() => json(uploadingJob(), 201)))();
    if (url === `/api/jobs/${JOB}/start`) return (overrides.start ?? (() => json(jobQueued, 202)))();
    if (url === `/api/jobs/${JOB}`) return (overrides.job ?? (() => json(uploadingJob())))();
    return json({ detail: "Not Found" }, 404);
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

function ResultStub() {
  const { jobId } = useParams();
  return <p data-testid="result-stub">{jobId}</p>;
}

/** A data router (SignalPage blocks in-app navigation with useBlocker while uploading). */
function renderPage(value: CapabilitiesState = ENABLED) {
  const router = createMemoryRouter(
    [
      {
        path: "/signal",
        element: (
          <>
            <Link to="/help" data-testid="go-help">
              Help
            </Link>
            <SignalPage />
          </>
        ),
      },
      { path: "/result/:jobId", element: <ResultStub /> },
      { path: "/help", element: <p data-testid="help-stub">help</p> },
      { path: "/", element: <p>home</p> },
    ],
    { initialEntries: ["/signal"] },
  );
  const utils = render(
    <CapabilitiesProvider value={value}>
      <RouterProvider router={router} />
    </CapabilitiesProvider>,
  );
  return { ...utils, router };
}

const mkFile = (name: string, bytes: number, lastModified = 1_700_000_000_000) =>
  new File([new Uint8Array(bytes)], name, { lastModified });

const FILES: Record<UploadSlot, File> = {
  pod5: mkFile("run.pod5", 40_000),
  bam: mkFile("run.bam", 9_000),
  reference: mkFile("ref.fa", 500),
  regions: mkFile("regions.csv", 90),
};

async function pickAll(user: ReturnType<typeof userEvent.setup>, files = FILES) {
  for (const slot of UPLOAD_SLOTS) await user.upload(screen.getByTestId(`upload-${slot}`), files[slot]);
}

function seedResumeRecord() {
  const entries = Object.fromEntries(
    UPLOAD_SLOTS.map((s) => [
      fileFingerprint(FILES[s]),
      { jobId: JOB, slot: s, uploadUrl: `/api/uploads/u-${s}`, savedAt: new Date().toISOString() },
    ]),
  );
  window.localStorage.setItem(RESUME_STORAGE_KEY, JSON.stringify(entries));
}

/** Start an upload with the pod5 and BAM PATCHes stalled, then press Pause. */
async function startAndPause(user: ReturnType<typeof userEvent.setup>) {
  tus.hold.add("/api/uploads/u-pod5");
  tus.hold.add("/api/uploads/u-bam");
  await pickAll(user);
  await user.click(screen.getByTestId("run"));
  await screen.findByTestId("upload-overall");
  await waitFor(() => expect(tus.log.filter((l) => l.startsWith("PATCH"))).toHaveLength(2));
  await user.click(screen.getByTestId("cancel"));
  await waitFor(() => expect(screen.getByTestId("upload-slot-pod5")).toHaveAttribute("data-status", "paused"));
  expect(screen.getByTestId("upload-slot-bam")).toHaveAttribute("data-status", "paused");
  expect(screen.getByTestId("upload-slot-reference")).toHaveAttribute("data-status", "waiting");
  expect(screen.getByTestId("upload-slot-regions")).toHaveAttribute("data-status", "waiting");
  tus.hold.clear();
}

beforeEach(() => {
  tus.offsets.clear();
  tus.log.length = 0;
  tus.hold.clear();
  window.localStorage.clear();
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
});
afterEach(() => vi.unstubAllGlobals());

describe("SignalPage", () => {
  it("shows the disabled notice when capabilities.signal is false", () => {
    stubApi();
    renderPage({ status: "ready", capabilities: DEFAULT_CAPABILITIES });
    expect(screen.getByTestId("signal-disabled")).toHaveTextContent("not enabled on this server");
  });

  it("says the server could not be reached (with Retry) when the capabilities are unavailable", () => {
    stubApi();
    renderPage({ status: "unavailable", capabilities: DEFAULT_CAPABILITIES, error: "Cannot reach the RModHub server." });
    const notice = screen.getByTestId("signal-unavailable");
    expect(notice).toHaveTextContent(/Could not reach the server/);
    expect(notice).toHaveTextContent("Cannot reach the RModHub server.");
    expect(notice).not.toHaveTextContent(/not enabled on this server/);
    expect(within(notice).getByTestId("signal-retry")).toBeEnabled();
    expect(screen.queryByTestId("signal-disabled")).not.toBeInTheDocument();
  });

  it("validates before /init: pod5 without BAM, wrong extension, size cap; Upload stays disabled", async () => {
    // applyAccept: false, so a wrongly named file reaches the page's own extension check.
    const user = userEvent.setup({ applyAccept: false });
    const { calls } = stubApi();
    renderPage({
      status: "ready",
      capabilities: { ...(capabilities as Capabilities), limits: { ...capabilities.limits, max_reference_mb: 0.0001 } },
    });
    expect(screen.getByTestId("run")).toBeDisabled();
    expect(screen.queryByTestId("local-error")).not.toBeInTheDocument();

    await user.upload(screen.getByTestId("upload-pod5"), FILES.pod5);
    expect(screen.getByTestId("upload-name-pod5")).toHaveTextContent(/run\.pod5 · 39 KB/);
    expect(screen.getByTestId("upload-error-bam")).toHaveTextContent(/A BAM file is required/);
    expect(screen.getByTestId("local-error")).toHaveTextContent(/pod5 file alone cannot be analysed/);
    expect(screen.getByTestId("run")).toBeDisabled();

    await user.upload(screen.getByTestId("upload-bam"), mkFile("reads.sam", 100));
    expect(screen.getByTestId("upload-error-bam")).toHaveTextContent('Expected a .bam file, got "reads.sam".');

    await user.upload(screen.getByTestId("upload-bam"), FILES.bam);
    await user.upload(screen.getByTestId("upload-reference"), FILES.reference);
    await user.upload(screen.getByTestId("upload-regions"), FILES.regions);
    expect(screen.queryByTestId("upload-error-bam")).not.toBeInTheDocument();
    expect(screen.getByTestId("upload-error-reference")).toHaveTextContent(/exceeds the 0\.0001 MB limit/);
    expect(screen.getByTestId("local-error")).toHaveTextContent("Fix the highlighted files before uploading.");
    expect(screen.getByTestId("run")).toBeDisabled();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("Load sample data posts /api/jobs/signal/sample and navigates to the result page", async () => {
    const user = userEvent.setup();
    const { calls } = stubApi();
    renderPage();
    await user.click(screen.getByTestId("load-sample"));
    expect(await screen.findByTestId("result-stub")).toHaveTextContent(jobQueued.job_id);
    expect(calls.find((c) => c.url === "/api/jobs/signal/sample")?.method).toBe("POST");
  });

  it("lists the sample files for download (synthetic, labelled)", async () => {
    stubApi();
    renderPage();
    const box = await screen.findByTestId("sample-files");
    expect(box).toHaveTextContent(/synthetic/);
    const links = within(box).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(samples.files.map((f) => f.url));
  });

  it("full flow: init -> tus upload of the four files (with progress) -> start -> /result/<id>", async () => {
    const user = userEvent.setup();
    const { calls } = stubApi();
    renderPage();
    await pickAll(user);
    expect(screen.queryByTestId("local-error")).not.toBeInTheDocument();
    const run = screen.getByTestId("run");
    expect(run).toBeEnabled();
    await user.click(screen.getByTestId("kit-RNA002"));
    await user.click(run);

    expect(await screen.findByTestId("result-stub")).toHaveTextContent(JOB);

    const init = calls.find((c) => c.url === "/api/jobs/signal/init")!;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body!)).toEqual({
      kit: "RNA002",
      files: {
        pod5: { name: "run.pod5", size: 40_000 },
        bam: { name: "run.bam", size: 9_000 },
        reference: { name: "ref.fa", size: 500 },
        regions: { name: "regions.csv", size: 90 },
      },
    });
    expect(calls.find((c) => c.url === `/api/jobs/${JOB}/start`)?.method).toBe("POST");
    for (const slot of UPLOAD_SLOTS) expect(tus.offsets.get(`/api/uploads/u-${slot}`)).toBe(FILES[slot].size);
    expect(tus.log.filter((l) => l.startsWith("PATCH"))).toHaveLength(4);
    expect(tus.log.filter((l) => l.startsWith("DELETE"))).toHaveLength(0);
    // The resume record is cleared once the job has started.
    expect(window.localStorage.getItem(RESUME_STORAGE_KEY)).toBeNull();
  });

  it("shows the 429 quota detail verbatim in the error banner", async () => {
    const user = userEvent.setup();
    const detail = "You already have 3 queued jobs; wait for one to finish.";
    stubApi({ init: () => json({ detail }, 429) });
    renderPage();
    await pickAll(user);
    await user.click(screen.getByTestId("run"));
    const banner = await screen.findByTestId("error");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveTextContent(detail);
    expect(screen.getByTestId("run")).toBeEnabled();
  });

  it("Pause -> 'Resume upload' continues the same job (no new /init, no DELETE) and starts it", async () => {
    const user = userEvent.setup();
    const { calls } = stubApi();
    renderPage();
    await startAndPause(user);

    // Nothing failed: no red banner, the primary button offers to resume.
    expect(screen.queryByTestId("error")).not.toBeInTheDocument();
    expect(screen.getByTestId("run")).toHaveTextContent("Resume upload");
    expect(screen.getByTestId("run")).toBeEnabled();
    expect(screen.getByTestId("resume-hint")).toHaveTextContent(/paused/);

    await user.click(screen.getByTestId("run"));
    expect(await screen.findByTestId("result-stub")).toHaveTextContent(JOB);
    expect(calls.filter((c) => c.url === "/api/jobs/signal/init")).toHaveLength(1);
    expect(calls.filter((c) => c.url === `/api/jobs/${JOB}/start`)).toHaveLength(1);
    expect(tus.log.filter((l) => l.startsWith("DELETE"))).toHaveLength(0);
    for (const slot of UPLOAD_SLOTS) expect(tus.offsets.get(`/api/uploads/u-${slot}`)).toBe(FILES[slot].size);
  });

  it("after Pause, Retry on one slot resumes every pending slot instead of showing a failure per slot", async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await startAndPause(user);
    const before = tus.log.length;

    await user.click(screen.getByTestId("upload-retry-pod5"));
    expect(await screen.findByTestId("result-stub")).toHaveTextContent(JOB);
    const after = tus.log.slice(before);
    for (const slot of UPLOAD_SLOTS) expect(after.some((l) => l.startsWith(`PATCH /api/uploads/u-${slot}`))).toBe(true);
    expect(screen.queryByTestId("error")).not.toBeInTheDocument();
  });

  it("picking a different file after a job was created cancels that job (DELETE) and the next Upload creates a new one", async () => {
    const user = userEvent.setup();
    const { calls } = stubApi();
    renderPage();
    await startAndPause(user);
    expect(window.localStorage.getItem(RESUME_STORAGE_KEY)).not.toBeNull();

    await user.upload(screen.getByTestId("upload-pod5"), mkFile("other.pod5", 41_000));
    await waitFor(() => expect(tus.log).toContain("DELETE /api/uploads/u-pod5"));
    expect(window.localStorage.getItem(RESUME_STORAGE_KEY)).toBeNull();
    expect(screen.getByTestId("run")).toHaveTextContent("Upload and start the job");
    expect(screen.queryByTestId("resume-hint")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("run"));
    expect(await screen.findByTestId("result-stub")).toHaveTextContent(JOB);
    expect(calls.filter((c) => c.url === "/api/jobs/signal/init")).toHaveLength(2);
  });

  it("offers to resume an earlier upload of the same four files and continues from the server's offsets", async () => {
    const user = userEvent.setup();
    seedResumeRecord();
    // The server already holds part of the pod5 and the whole regions file.
    tus.offsets.set("/api/uploads/u-pod5", 16_000);
    tus.offsets.set("/api/uploads/u-regions", 90);
    const { calls } = stubApi({ job: () => json(uploadingJob({ pod5: 16_000, regions: 90 })) });
    renderPage();
    await pickAll(user);
    const prompt = await screen.findByTestId("resume-prompt");
    expect(prompt).toHaveTextContent(/Resume it where it stopped/);
    await user.click(screen.getByTestId("resume-yes"));
    expect(await screen.findByTestId("result-stub")).toHaveTextContent(JOB);
    expect(calls.some((c) => c.url === "/api/jobs/signal/init")).toBe(false);
    expect(tus.log).toContain("PATCH /api/uploads/u-pod5 16000");
    expect(tus.log.filter((l) => l.startsWith("PATCH /api/uploads/u-regions"))).toHaveLength(0);
    for (const slot of UPLOAD_SLOTS) expect(tus.offsets.get(`/api/uploads/u-${slot}`)).toBe(FILES[slot].size);
  });

  it("'Discard it and start a new job' cancels the earlier job on the server and forgets it", async () => {
    const user = userEvent.setup();
    seedResumeRecord();
    stubApi();
    renderPage();
    await pickAll(user);
    await user.click(await screen.findByTestId("resume-no"));
    await waitFor(() => expect(screen.queryByTestId("resume-prompt")).not.toBeInTheDocument());
    await waitFor(() => expect(tus.log).toContain("DELETE /api/uploads/u-pod5"));
    expect(window.localStorage.getItem(RESUME_STORAGE_KEY)).toBeNull();
    expect(screen.getByTestId("run")).toBeEnabled();
    expect(screen.getByTestId("run")).toHaveTextContent("Upload and start the job");
  });

  it("resume: a network error keeps the record and the prompt instead of deleting them", async () => {
    const user = userEvent.setup();
    seedResumeRecord();
    stubApi({
      job: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    renderPage();
    await pickAll(user);
    await user.click(await screen.findByTestId("resume-yes"));
    const banner = await screen.findByTestId("error");
    expect(banner).toHaveTextContent(/Could not check the earlier upload/);
    expect(banner).toHaveTextContent(/Cannot reach the RModHub server/);
    expect(screen.getByTestId("resume-prompt")).toBeInTheDocument();
    expect(window.localStorage.getItem(RESUME_STORAGE_KEY)).not.toBeNull();
    expect(screen.queryByTestId("result-stub")).not.toBeInTheDocument();
  });

  it("resume: a cancelled earlier job stays on the page with the picked files and clears the record", async () => {
    const user = userEvent.setup();
    seedResumeRecord();
    stubApi({ job: () => json({ ...jobCancelled, job_id: JOB }) });
    renderPage();
    await pickAll(user);
    await user.click(await screen.findByTestId("resume-yes"));
    const banner = await screen.findByTestId("error");
    expect(banner).toHaveTextContent(/was cancelled/);
    expect(screen.queryByTestId("resume-prompt")).not.toBeInTheDocument();
    expect(screen.queryByTestId("result-stub")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(RESUME_STORAGE_KEY)).toBeNull();
    for (const slot of UPLOAD_SLOTS) expect(screen.getByTestId(`upload-name-${slot}`)).toHaveTextContent(FILES[slot].name);
    expect(screen.getByTestId("run")).toBeEnabled();
    expect(screen.getByTestId("run")).toHaveTextContent("Upload and start the job");
  });

  it("leaving the page mid-upload asks for confirmation (in-app navigation and beforeunload)", async () => {
    const user = userEvent.setup();
    tus.hold.add("/api/uploads/u-pod5");
    stubApi();
    renderPage();
    await pickAll(user);
    await user.click(screen.getByTestId("run"));
    await screen.findByTestId("upload-overall");

    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);

    await user.click(screen.getByTestId("go-help"));
    const prompt = await screen.findByTestId("leave-prompt");
    expect(prompt).toHaveTextContent(/An upload is in progress/);
    expect(screen.queryByTestId("help-stub")).not.toBeInTheDocument();
    expect(screen.getByTestId("upload-overall")).toBeInTheDocument();

    await user.click(screen.getByTestId("leave-stay"));
    await waitFor(() => expect(screen.queryByTestId("leave-prompt")).not.toBeInTheDocument());
    expect(screen.getByTestId("upload-overall")).toBeInTheDocument();

    await user.click(screen.getByTestId("go-help"));
    await user.click(await screen.findByTestId("leave-confirm"));
    expect(await screen.findByTestId("help-stub")).toBeInTheDocument();
  });

  it("announces upload progress through one throttled live region, not per progress tick", async () => {
    const user = userEvent.setup();
    tus.hold.add("/api/uploads/u-pod5");
    stubApi();
    renderPage();
    await pickAll(user);
    await user.click(screen.getByTestId("run"));
    await screen.findByTestId("upload-overall");
    expect(screen.getByTestId("upload-overall")).not.toHaveAttribute("aria-live");
    expect(screen.getByTestId("upload-status-pod5")).not.toHaveAttribute("aria-live");
    const live = screen.getByTestId("signal-page").querySelector("form")!.querySelectorAll("[aria-live]");
    expect(live).toHaveLength(1);
    expect(live[0]).toHaveAttribute("data-testid", "upload-announcer");
    expect(live[0]).toHaveTextContent(/^Upload \d0% overall: pod5 uploading, BAM \w+, reference \w+, regions \w+\.$/);
  });
});

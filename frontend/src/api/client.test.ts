import { afterEach, describe, expect, it, vi } from "vitest";
import capabilities from "./fixtures/capabilities.json";
import errChars from "./fixtures/err_chars.json";
import errShort from "./fixtures/err_short.json";
import jobRunning from "./fixtures/job_running.json";
import {
  ApiError,
  cancelJob,
  createSampleJob,
  describeError,
  getCapabilities,
  getJob,
  getJobResults,
  getJobResultsCsv,
  initSignalJob,
  predictSequence,
  SIGNAL_DISABLED_MESSAGE,
  signalCsvFilename,
} from "./client";

function mockFetch(status: number, body: unknown, headers: Record<string, string> = {}) {
  // A fresh Response per call: bodies can only be read once.
  const fn = vi.fn(
    async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } }),
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("API error mapping", () => {
  it("surfaces the backend's plain-language 422 detail", async () => {
    mockFetch(errShort.status, errShort.body);
    await expect(predictSequence({ sequence: "ACGT" })).rejects.toMatchObject({
      status: 422,
      detail: expect.stringContaining("at least 51"),
    });
    mockFetch(errChars.status, errChars.body);
    const err = await predictSequence({ sequence: "ACGTN" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(describeError(err)).toContain("'N'");
  });

  it("replaces 503 with a model-loading message on the sequence endpoint", async () => {
    mockFetch(503, { detail: "model not loaded" });
    const err = await predictSequence({ sequence: "ACGT" }).catch((e: unknown) => e);
    expect(describeError(err)).toMatch(/still loading/);
  });

  it("413 is endpoint-neutral (no sequence-specific wording)", async () => {
    mockFetch(413, { detail: "Request Entity Too Large" });
    const err = await predictSequence({ sequence: "ACGT" }).catch((e: unknown) => e);
    expect(describeError(err)).toBe("The request is too large for this server.");
    expect(describeError(err)).not.toMatch(/10,000/);
  });

  it("explains network failures without leaking internals", () => {
    expect(describeError(new TypeError("Failed to fetch"))).toMatch(/Cannot reach/);
    expect(describeError(new DOMException("aborted", "AbortError"))).toBe("Request cancelled.");
  });
});

describe("signal-branch client", () => {
  it("GET /api/capabilities", async () => {
    const fn = mockFetch(200, capabilities);
    const caps = await getCapabilities();
    expect(caps.signal).toBe(true);
    expect(caps.limits.max_pod5_gb).toBe(5);
    expect(String(fn.mock.calls[0][0])).toBe("/api/capabilities");
  });

  it("GET /api/capabilities carries the optional BAM cap and upload TTL when the API reports them", async () => {
    mockFetch(200, { ...capabilities, limits: { ...capabilities.limits, max_bam_gb: 2.5, upload_ttl_h: 12 } });
    const caps = await getCapabilities();
    expect(caps.limits.max_bam_gb).toBe(2.5);
    expect(caps.limits.upload_ttl_h).toBe(12);
    mockFetch(200, capabilities); // an older API without them: the fields are simply absent
    const older = await getCapabilities();
    expect(older.limits.upload_ttl_h).toBeUndefined();
  });

  it("503 on a signal endpoint keeps the server's sentence, or says the branch is disabled", async () => {
    mockFetch(503, { detail: "The nanopore signal branch is not enabled on this server." });
    let err = await createSampleJob().catch((e: unknown) => e);
    expect(describeError(err)).toBe(SIGNAL_DISABLED_MESSAGE);
    mockFetch(503, {});
    err = await createSampleJob().catch((e: unknown) => e);
    expect(describeError(err)).toBe(SIGNAL_DISABLED_MESSAGE);
    expect((err as ApiError).status).toBe(503);
  });

  it("429 quota detail is shown verbatim and 404 jobs get a readable default", async () => {
    mockFetch(429, { detail: "You already have 3 queued jobs; wait for one to finish." });
    const err = await initSignalJob({
      kit: "RNA004",
      files: { pod5: { name: "a.pod5", size: 1 }, bam: { name: "a.bam", size: 1 }, reference: { name: "r.fa", size: 1 }, regions: { name: "r.csv", size: 1 } },
    }).catch((e: unknown) => e);
    expect(describeError(err)).toBe("You already have 3 queued jobs; wait for one to finish.");
    mockFetch(404, {});
    const missing = await getJob("6f1d2c3a-9b8e-4c7d-a1b2-3c4d5e6f7a8b").catch((e: unknown) => e);
    expect((missing as ApiError).status).toBe(404);
    expect(describeError(missing)).toBe("This job is unknown or has expired.");
  });

  it("builds the job/result/cancel URLs and query strings per the contract", async () => {
    const fn = mockFetch(200, jobRunning);
    const id = jobRunning.job_id;
    await getJob(id);
    expect(String(fn.mock.calls[0][0])).toBe(`/api/jobs/${id}`);
    await cancelJob(id);
    expect(String(fn.mock.calls[1][0])).toBe(`/api/jobs/${id}/cancel`);
    expect((fn.mock.calls[1][1] as RequestInit).method).toBe("POST");
    await getJobResults(id, { level: "read", transcript_id: "tx_A", position: 101, mod_type: "m6A", offset: 25, limit: 25 });
    expect(String(fn.mock.calls[2][0])).toBe(
      `/api/jobs/${id}/results?level=read&transcript_id=tx_A&position=101&mod_type=m6A&offset=25&limit=25`,
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("a,b\n", { status: 200, headers: { "Content-Type": "text/csv" } })));
    const blob = await getJobResultsCsv(id, "site");
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.size).toBe(4); // jsdom's Blob has no text(); size is enough to prove the body came through
    expect(signalCsvFilename(id, "site")).toBe(`rmodhub_signal_${id}_sites.csv`);
    expect(signalCsvFilename(id, "read")).toBe(`rmodhub_signal_${id}_reads.csv`);
  });

  it("passes the strand filter and encodes '+' as %2B (a bare '+' would decode to a space)", async () => {
    const fn = mockFetch(200, { results: [], meta: {}, total: 0, offset: 0, limit: 25 });
    const id = jobRunning.job_id;
    await getJobResults(id, { level: "read", transcript_id: "tx_B", position: 81, mod_type: "m5C", strand: "+", offset: 0, limit: 25 });
    expect(String(fn.mock.calls[0][0])).toBe(
      `/api/jobs/${id}/results?level=read&transcript_id=tx_B&position=81&mod_type=m5C&strand=%2B&offset=0&limit=25`,
    );
    await getJobResults(id, { level: "site", strand: "-" });
    expect(String(fn.mock.calls[1][0])).toBe(`/api/jobs/${id}/results?level=site&strand=-`);
    await getJobResults(id, { level: "site", strand: "" }); // blank = no filter, like the API
    expect(String(fn.mock.calls[2][0])).toBe(`/api/jobs/${id}/results?level=site`);
  });
});

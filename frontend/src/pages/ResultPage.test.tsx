import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import capabilities from "../api/fixtures/capabilities.json";
import jobCancelled from "../api/fixtures/job_cancelled.json";
import jobDone from "../api/fixtures/job_done.json";
import jobFailed from "../api/fixtures/job_failed.json";
import jobRunning from "../api/fixtures/job_running.json";
import signalReads from "../api/fixtures/signal_reads.json";
import signalResults from "../api/fixtures/signal_results.json";
import type { Capabilities } from "../api/types";
import { CapabilitiesProvider, DEFAULT_CAPABILITIES, type CapabilitiesState } from "../components/layout/CapabilitiesProvider";
import { ResultPage } from "./ResultPage";

const ID = jobDone.job_id;
const ENABLED = { status: "ready" as const, capabilities: capabilities as Capabilities };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

interface RouterOptions {
  job?: unknown;
  jobStatus?: number;
  results?: (offset: number) => unknown;
  cancel?: unknown;
}

/** fetch stub keyed by the contract's paths. */
function stubApi(opts: RouterOptions = {}) {
  const calls: { url: string; method: string }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, method: init?.method ?? "GET" });
    if (url.endsWith("/cancel")) return json(opts.cancel ?? jobCancelled);
    if (url.includes("/results?")) {
      const q = new URL(url, "http://localhost").searchParams;
      if (q.get("level") === "read") return json(signalReads);
      return json(opts.results ? opts.results(Number(q.get("offset") ?? 0)) : signalResults);
    }
    if (url.endsWith(`/api/jobs/${ID}`)) return json(opts.job ?? jobDone, opts.jobStatus ?? 200);
    return json({ detail: "Not Found" }, 404);
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

function renderPage(path = `/result/${ID}`, value: CapabilitiesState = ENABLED) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <CapabilitiesProvider value={value}>
        <Routes>
          <Route path="/result/:jobId" element={<ResultPage />} />
          <Route path="/signal" element={<p>signal page</p>} />
        </Routes>
      </CapabilitiesProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => vi.unstubAllGlobals());

describe("ResultPage", () => {
  it("done: summary, coverage warning, transcript selector, signal columns, read-level drill-down", async () => {
    const user = userEvent.setup();
    const { calls } = stubApi();
    renderPage();

    const status = await screen.findByTestId("job-status");
    expect(status).toHaveAttribute("data-status", "done");
    expect(screen.getByTestId("job-status-pill")).toHaveTextContent("Done");
    // A finished job carries no stage (the worker clears it): say so instead of "—".
    expect(screen.getByTestId("job-stage")).toHaveTextContent("Stage: Finished");
    expect(screen.getByTestId("job-cancel")).toBeDisabled();
    expect(screen.getByTestId("copy-link")).toBeInTheDocument();
    expect(screen.getByTestId("job-expires")).not.toHaveTextContent("—");
    expect(screen.getByTestId("job-inputs")).toHaveTextContent(/pod5 819 KB/);

    // Results are fetched page-wise with the maximum limit.
    await screen.findByTestId("signal-summary");
    const resultCalls = calls.filter((c) => c.url.includes("/results?"));
    expect(resultCalls[0].url).toBe(`/api/jobs/${ID}/results?level=site&offset=0&limit=1000`);
    expect(screen.getByTestId("n-sites")).toHaveTextContent("22");
    expect(screen.getByTestId("regions-summary")).toHaveTextContent(/3 given · 1 skipped/);

    const warning = screen.getByTestId("coverage-warning");
    expect(warning).toHaveTextContent("4 of 22 sites have coverage below 30 reads");
    expect(warning).toHaveTextContent(/unreliable/);

    const select = screen.getByTestId("transcript-select");
    expect(within(select).getAllByRole("option")).toHaveLength(2);
    expect(select).toHaveValue("tx_A");

    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toEqual(expect.arrayContaining(["Coverage", "95% CI", "Strand", "Modified reads"]));
    expect(headers).not.toContain("p-value");
    expect(screen.getAllByTestId("result-row")).toHaveLength(14);
    // The server CSV spans the whole job (22 sites), not the 14 rows of the shown transcript.
    expect(screen.getByTestId("visible-count")).toHaveTextContent("Showing 14 of 14 sites");
    expect(screen.getByTestId("download-csv")).toHaveTextContent("Download CSV (all 22 sites)");
    expect(screen.getByTestId("download-csv-note")).toHaveTextContent(/all 22 sites of every transcript/);
    // Only the six DirectRM types are offered as chips, and there is no p-value filter.
    expect(screen.getByTestId("results-toolbar")).toHaveTextContent("(6/6 selected)");
    expect(screen.queryByTestId("filter-mod-type-Am")).not.toBeInTheDocument();
    expect(screen.queryByTestId("filter-pvalue-max")).not.toBeInTheDocument();
    expect(screen.getByTestId("track-view")).toBeInTheDocument();
    expect(screen.getByTestId("track-legend")).toHaveTextContent(/modification rate/);
    expect(screen.getByTestId("data-lifecycle")).toBeInTheDocument();
    expect(screen.queryByTestId("read-panel")).not.toBeInTheDocument();

    // Row click -> read-level panel (server-paged, 25 of 40 reads).
    await user.click(screen.getByTestId("results-table").querySelector('[data-key="101:m6A:+"]')!);
    const panel = await screen.findByTestId("read-panel");
    expect(await within(panel).findAllByTestId("read-row")).toHaveLength(25);
    expect(panel).toHaveTextContent(/rows 1–25 of 40/);
    expect(panel).toHaveTextContent(/31 of 40 reads called modified/);
    const readCall = calls.find((c) => c.url.includes("level=read"));
    expect(readCall?.url).toBe(
      `/api/jobs/${ID}/results?level=read&transcript_id=tx_A&position=101&mod_type=m6A&strand=%2B&offset=0&limit=25`,
    );
    expect(within(panel).getAllByTestId("read-row").filter((r) => r.getAttribute("data-called") === "true").length).toBeGreaterThan(0);

    // Switching transcript shows its rows and clears the selection.
    await user.selectOptions(select, "tx_B");
    expect(screen.getAllByTestId("result-row")).toHaveLength(8);
    expect(screen.queryByTestId("read-panel")).not.toBeInTheDocument();
    expect(screen.getByTestId("track-view")).toHaveTextContent(/of 900 nt/);
  });

  it("pages through the site rows until `total` is reached", async () => {
    const first = { ...signalResults, total: 30 };
    const extra = {
      ...signalResults,
      total: 30,
      offset: 22,
      results: signalResults.results.slice(0, 8).map((r) => ({ ...r, transcript_id: "tx_A", position: r.position + 1 })),
    };
    const { calls } = stubApi({ results: (offset) => (offset === 0 ? first : extra) });
    renderPage();
    await screen.findByTestId("signal-summary");
    await waitFor(() => expect(calls.filter((c) => c.url.includes("level=site"))).toHaveLength(2));
    expect(calls.filter((c) => c.url.includes("level=site"))[1].url).toContain("offset=22");
    await waitFor(() => expect(screen.getAllByTestId("result-row")).toHaveLength(22));
  });

  it("running: stage explanation, progress, ETA; Cancel posts and adopts the cancelled status", async () => {
    const user = userEvent.setup();
    const { calls } = stubApi({ job: jobRunning });
    renderPage();
    const status = await screen.findByTestId("job-status");
    expect(status).toHaveAttribute("data-status", "running");
    expect(screen.getByTestId("job-stage")).toHaveTextContent("Extracting features");
    expect(screen.getByTestId("job-stage")).toHaveTextContent(/k-mer signal features/);
    expect(screen.getByTestId("job-progress")).toHaveAttribute("aria-valuenow", "42");
    expect(screen.getByTestId("job-eta")).toHaveTextContent("1 min 35 s");
    expect(screen.getByTestId("job-elapsed")).toHaveTextContent(/Elapsed:/);
    expect(screen.queryByTestId("results")).not.toBeInTheDocument();

    const cancel = screen.getByTestId("job-cancel");
    expect(cancel).toBeEnabled();
    await user.click(cancel);
    await waitFor(() => expect(screen.getByTestId("job-status")).toHaveAttribute("data-status", "cancelled"));
    expect(calls.some((c) => c.method === "POST" && c.url.endsWith(`/api/jobs/${ID}/cancel`))).toBe(true);
    expect(screen.getByTestId("job-cancel")).toBeDisabled();
    expect(screen.getByTestId("job-cancelled")).toBeInTheDocument();
  });

  it("failed: shows the job's error sentence", async () => {
    stubApi({ job: jobFailed });
    renderPage();
    expect(await screen.findByTestId("job-error")).toHaveTextContent(/dorado --emit-moves/);
    expect(screen.getByTestId("job-status")).toHaveAttribute("data-status", "failed");
    expect(screen.getByTestId("job-cancel")).toBeDisabled();
  });

  it("uploading: per-slot offsets from job.uploads", async () => {
    const uploading = await import("../api/fixtures/job_uploading.json");
    stubApi({ job: uploading.default });
    renderPage();
    const list = await screen.findByTestId("job-uploads");
    expect(within(list).getByTestId("job-upload-pod5")).toHaveTextContent(/328 KB \/ 819 KB \(40%\)/);
    expect(within(list).getByTestId("job-upload-bam")).toHaveTextContent(/complete/);
  });

  it("a transcript without sites explains the skipped region instead of 'no sites match the filters'", async () => {
    const user = userEvent.setup();
    const withSkipped = {
      ...signalResults,
      meta: {
        ...signalResults.meta,
        n_transcripts: 3,
        transcripts: [...signalResults.meta.transcripts, { transcript_id: "tx_C", length: 579, n_reads: 12, n_sites: 0 }],
        extra: { ...signalResults.meta.extra, regions_total: 3, regions_skipped_low_coverage: 1, min_coverage: 30 },
      },
    };
    stubApi({ results: () => withSkipped });
    renderPage();
    const select = await screen.findByTestId("transcript-select");
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toHaveLength(3);
    expect(options[2]).toMatch(/tx_C — 579 nt, 12 reads, 0 sites \(skipped: too few reads\)/);

    await user.selectOptions(select, "tx_C");
    const notice = screen.getByTestId("transcript-empty");
    expect(notice).toHaveTextContent(/No site was called on tx_C/);
    expect(notice).toHaveTextContent(/12 reads/);
    expect(notice).toHaveTextContent(/30 or fewer/);
    expect(screen.queryByTestId("results-table")).not.toBeInTheDocument();
    expect(screen.queryByTestId("results-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("track-view")).not.toBeInTheDocument();

    await user.selectOptions(select, "tx_A");
    expect(screen.getAllByTestId("result-row")).toHaveLength(14);
  });

  it("signal branch disabled (capabilities): says so and never polls the job", () => {
    const { fn } = stubApi();
    renderPage(undefined, { status: "ready", capabilities: DEFAULT_CAPABILITIES });
    expect(screen.getByTestId("signal-disabled")).toHaveTextContent(/not enabled on this server/);
    expect(screen.getByTestId("job-unavailable")).toHaveTextContent(ID);
    expect(fn).not.toHaveBeenCalled();
    expect(screen.queryByTestId("job-loading")).not.toBeInTheDocument();
  });

  it("503 from the job route (branch disabled) -> the disabled notice, not an endless retry banner", async () => {
    const detail = "The nanopore signal branch is not enabled on this server.";
    const { fn } = stubApi({ jobStatus: 503, job: { detail } });
    renderPage(undefined, { status: "unavailable", capabilities: DEFAULT_CAPABILITIES });
    expect(await screen.findByTestId("signal-disabled")).toHaveTextContent(detail);
    expect(screen.queryByTestId("error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("job-missing")).not.toBeInTheDocument();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("404 -> unknown or expired job", async () => {
    stubApi({ jobStatus: 404, job: { detail: "Unknown or expired job." } });
    renderPage();
    const missing = await screen.findByTestId("job-missing");
    expect(missing).toHaveTextContent(/Unknown or expired job/);
    expect(missing).toHaveTextContent(/14 days/);
    expect(missing).toHaveTextContent(/expire after 48 h/);
    expect(screen.queryByTestId("job-status")).not.toBeInTheDocument();
  });

  it("a malformed id is rejected without touching the server", () => {
    const { fn } = stubApi();
    renderPage("/result/not-a-job");
    expect(screen.getByTestId("job-missing")).toHaveTextContent(/not a valid job link/);
    expect(fn).not.toHaveBeenCalled();
  });
});

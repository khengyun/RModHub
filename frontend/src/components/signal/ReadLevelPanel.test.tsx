import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SignalRead, SignalSite } from "../../api/types";
import { ReadLevelPanel } from "./ReadLevelPanel";

vi.mock("../../lib/download", () => ({ downloadBlob: vi.fn(), downloadText: vi.fn() }));

const JOB = "6f1d2c3a-9b8e-4c7d-a1b2-3c4d5e6f7a8b";

/** tx_B is listed on both strands in the regions file: one m5C site per strand at base 81. */
function site(strand: "+" | "-"): SignalSite {
  const plus = strand === "+";
  return {
    transcript_id: "tx_B",
    position: 81,
    mod_type: "m5C",
    probability: plus ? 0.75 : 0.2,
    p_value: null,
    coverage: plus ? 36 : 40,
    source: "signal",
    strand,
    count: plus ? 27 : 8,
    ci_low: plus ? 0.59 : 0.105,
    ci_high: plus ? 0.86 : 0.348,
    max_prob: 0.99,
    noisyor_prob: 1,
  };
}

const READS: SignalRead[] = [
  { read_id: "r4", transcript_id: "tx_B", position: 81, strand: "+", mod_type: "m5C", probability: 0.99, source: "signal" },
  { read_id: "r5", transcript_id: "tx_B", position: 81, strand: "-", mod_type: "m5C", probability: 0.3, source: "signal" },
  { read_id: "r6", transcript_id: "tx_B", position: 81, strand: "-", mod_type: "m5C", probability: 0.85, source: "signal" },
];

/** fetch stub that applies the `strand` query parameter the way the server does. */
function stubReads() {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      const q = new URL(url, "http://localhost").searchParams;
      const strand = q.get("strand"); // %2B decodes back to "+"
      const rows = READS.filter((r) => strand === null || r.strand === strand);
      const body = { results: rows, meta: {}, total: rows.length, offset: 0, limit: 25 };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return urls;
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => vi.unstubAllGlobals());

describe("ReadLevelPanel", () => {
  it("asks for the reads of the selected site's strand only", async () => {
    const urls = stubReads();
    const { rerender } = render(<ReadLevelPanel jobId={JOB} site={site("+")} onClose={vi.fn()} />);
    const panel = await screen.findByTestId("read-panel");
    expect(panel).toHaveTextContent("Reads at tx_B:81 (+)");
    expect(panel).toHaveTextContent(/27 of 36 reads called modified/);
    const rows = await within(panel).findAllByTestId("read-row");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("r4");
    expect(urls).toEqual([
      `/api/jobs/${JOB}/results?level=read&transcript_id=tx_B&position=81&mod_type=m5C&strand=%2B&offset=0&limit=25`,
    ]);

    // The minus-strand site of the same base: a new request, only its own reads.
    rerender(<ReadLevelPanel jobId={JOB} site={site("-")} onClose={vi.fn()} />);
    expect(await screen.findByText("Reads at tx_B:81 (-)", { exact: false })).toBeInTheDocument();
    await screen.findByText("r5");
    const minusRows = within(screen.getByTestId("read-panel")).getAllByTestId("read-row");
    expect(minusRows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("r5"),
      expect.stringContaining("r6"),
    ]);
    expect(minusRows.map((r) => r.getAttribute("data-called"))).toEqual(["false", "true"]);
    expect(urls[1]).toBe(
      `/api/jobs/${JOB}/results?level=read&transcript_id=tx_B&position=81&mod_type=m5C&strand=-&offset=0&limit=25`,
    );
    expect(screen.getByTestId("read-panel")).toHaveTextContent(/8 of 40 reads called modified/);
  });

  it("shows the server's sentence when the reads cannot be loaded", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "The results file of this job is missing." }), { status: 404 })),
    );
    render(<ReadLevelPanel jobId={JOB} site={site("+")} onClose={vi.fn()} />);
    expect(await screen.findByTestId("read-error")).toHaveTextContent("The results file of this job is missing.");
  });
});

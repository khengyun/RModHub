import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { siteKey, type ModSite, type PredictionMeta, type SignalSite } from "../../api/types";
import golden from "../../api/fixtures/golden_attention.json";
import manyRows from "../../api/fixtures/many_rows.json";
import signalResults from "../../api/fixtures/signal_results.json";
import { transcriptMeta } from "../signal/signalModel";
import { ResultsTable, type CsvSource } from "./ResultsTable";

vi.mock("../../lib/download", () => ({ downloadBlob: vi.fn(), downloadText: vi.fn() }));

import { downloadBlob, downloadText } from "../../lib/download";

const rows = golden.results as ModSite[];
const meta = golden.meta as unknown as PredictionMeta;
const many = manyRows.response.results as ModSite[];
const manyMeta = manyRows.response.meta as unknown as PredictionMeta;

function makeCsv(): CsvSource & { download: ReturnType<typeof vi.fn> } {
  return { download: vi.fn(), filename: "rmodhub_sites_sequence_151nt.csv" };
}

function renderTable(props: Partial<React.ComponentProps<typeof ResultsTable>> = {}) {
  const onSelect = vi.fn();
  const onVisibleChange = vi.fn();
  const csv = makeCsv();
  const utils = render(
    <ResultsTable
      sites={rows}
      meta={meta}
      csv={csv}
      selectedKey={null}
      onSelect={onSelect}
      onVisibleChange={onVisibleChange}
      {...props}
    />,
  );
  return { ...utils, onSelect, onVisibleChange, csv: (props.csv as typeof csv | undefined) ?? csv };
}

const rowKeys = () => screen.getAllByTestId("result-row").map((tr) => tr.getAttribute("data-key"));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ResultsTable (golden fixture)", () => {
  it("renders all 22 rows on one page with the count text and row metadata", () => {
    const { onVisibleChange } = renderTable();
    expect(screen.getByTestId("results-table")).toBeInTheDocument();
    const trs = screen.getAllByTestId("result-row");
    expect(trs).toHaveLength(22);
    expect(screen.getByTestId("visible-count")).toHaveTextContent(/^Showing 22 of 22 sites$/);
    expect(screen.getByTestId("page-info")).toHaveTextContent("Page 1 of 1");
    expect(trs[0]).toHaveAttribute("data-key", "52:Gm");
    expect(trs[0]).toHaveAttribute("data-position", "52");
    expect(trs[0]).toHaveAttribute("data-mod-type", "Gm");
    expect(trs[0]).toHaveAttribute("aria-selected", "false");
    // Row 1: index, position, badge, probability (3 dp), p-value.
    const cells = within(trs[0]).getAllByRole("cell").map((td) => td.textContent);
    expect(cells).toEqual(["1", "52", "Gm", "0.555", "0.0267"]);
    // Position 79 keeps both rows.
    expect(rowKeys().filter((k) => k?.startsWith("79:"))).toEqual(["79:Cm", "79:m5C"]);
    // Sequence branch: no transcript / strand / coverage / CI / count columns.
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toEqual(["#", "Position", "Modification", "Probability", "p-value"]);
    // The page is told which rows are visible (all of them, in table order).
    expect(onVisibleChange).toHaveBeenCalledTimes(1);
    expect(onVisibleChange.mock.calls[0][0].map(siteKey)).toEqual(rows.map(siteKey));
  });

  it("clicking a row selects it; clicking the selected row again clears the selection", async () => {
    const user = userEvent.setup();
    const { onSelect, rerender, csv } = renderTable();
    await user.click(screen.getAllByTestId("result-row")[0]);
    expect(onSelect).toHaveBeenCalledWith("52:Gm");

    rerender(<ResultsTable sites={rows} meta={meta} csv={csv} selectedKey="52:Gm" onSelect={onSelect} />);
    const first = screen.getAllByTestId("result-row")[0];
    expect(first).toHaveAttribute("aria-selected", "true");
    await user.click(first);
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("keyboard: Enter on a focused row selects it", () => {
    const { onSelect } = renderTable();
    fireEvent.keyDown(screen.getAllByTestId("result-row")[1], { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith("63:m5C");
  });

  it("modification chips filter rows and report the visible set", async () => {
    const user = userEvent.setup();
    const { onVisibleChange } = renderTable();
    // Types absent from the results are disabled with count 0.
    expect(screen.getByTestId("filter-mod-type-Am")).toBeDisabled();
    expect(screen.getByTestId("filter-mod-type-Am")).toHaveTextContent("0");
    expect(screen.getByTestId("filter-mod-type-m5C")).toHaveTextContent("6");

    await user.click(screen.getByTestId("filter-mod-type-none"));
    expect(screen.getByTestId("results-empty")).toHaveTextContent("No sites match the current filters");
    expect(screen.getByTestId("visible-count")).toHaveTextContent("Showing 0 of 22 sites");

    await user.click(screen.getByTestId("filter-mod-type-m5C"));
    expect(screen.getByTestId("filter-mod-type-m5C")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getAllByTestId("result-row")).toHaveLength(6);
    expect(screen.getByTestId("visible-count")).toHaveTextContent("Showing 6 of 22 sites");
    expect(onVisibleChange).toHaveBeenLastCalledWith(expect.arrayContaining([]));
    expect(onVisibleChange.mock.lastCall?.[0]).toHaveLength(6);

    await user.click(screen.getByTestId("filter-mod-type-all"));
    expect(screen.getAllByTestId("result-row")).toHaveLength(22);
  });

  it("numeric filters, quick filter and reset", async () => {
    const user = userEvent.setup();
    renderTable();
    expect(screen.getByTestId("filter-pvalue-max")).toHaveValue(0.05);
    expect(screen.getByTestId("filter-pos-min")).toHaveValue(26);
    expect(screen.getByTestId("filter-pos-max")).toHaveValue(126);

    fireEvent.change(screen.getByTestId("filter-pvalue-max"), { target: { value: "0.03" } });
    expect(screen.getAllByTestId("result-row")).toHaveLength(11);

    fireEvent.change(screen.getByTestId("filter-pvalue-max"), { target: { value: "" } }); // no limit
    fireEvent.change(screen.getByTestId("filter-prob-min"), { target: { value: "0.5" } });
    expect(screen.getAllByTestId("result-row").every((tr) => Number(tr.getAttribute("data-position")) > 0)).toBe(true);
    const n = screen.getAllByTestId("result-row").length;
    expect(n).toBeGreaterThan(0);
    expect(n).toBeLessThan(22);

    fireEvent.change(screen.getByTestId("filter-prob-min"), { target: { value: "0" } });
    fireEvent.change(screen.getByTestId("filter-pos-min"), { target: { value: "79" } });
    fireEvent.change(screen.getByTestId("filter-pos-max"), { target: { value: "79" } });
    expect(rowKeys()).toEqual(["79:Cm", "79:m5C"]);

    fireEvent.change(screen.getByTestId("filter-pos-min"), { target: { value: "200" } });
    expect(screen.getByTestId("results-empty")).toBeInTheDocument();
    await user.click(screen.getByTestId("filter-reset-empty"));
    expect(screen.getAllByTestId("result-row")).toHaveLength(22);

    await user.type(screen.getByTestId("filter-text"), "psi");
    expect(rowKeys()).toEqual(["123:Psi"]);
    await user.click(screen.getByTestId("filter-reset"));
    expect(screen.getByTestId("filter-text")).toHaveValue("");
    expect(screen.getAllByTestId("result-row")).toHaveLength(22);
    expect(screen.getByTestId("visible-count")).toHaveTextContent("Showing 22 of 22 sites");
  });

  it("sorting: header buttons toggle asc/desc and expose aria-sort", async () => {
    const user = userEvent.setup();
    renderTable();
    const positionTh = screen.getByTestId("sort-position").closest("th");
    expect(positionTh).toHaveAttribute("aria-sort", "ascending");

    await user.click(screen.getByTestId("sort-p_value"));
    expect(screen.getByTestId("sort-p_value").closest("th")).toHaveAttribute("aria-sort", "ascending");
    expect(positionTh).toHaveAttribute("aria-sort", "none");
    expect(rowKeys()[0]).toBe("107:Gm");

    await user.click(screen.getByTestId("sort-p_value"));
    expect(screen.getByTestId("sort-p_value").closest("th")).toHaveAttribute("aria-sort", "descending");
    expect(rowKeys()[0]).not.toBe("107:Gm");

    await user.click(screen.getByTestId("sort-probability"));
    await user.click(screen.getByTestId("sort-probability"));
    const maxProb = Math.max(...rows.map((r) => r.probability));
    const first = rows.find((r) => r.probability === maxProb)!;
    expect(rowKeys()[0]).toBe(siteKey(first));

    await user.click(screen.getByTestId("sort-mod_type"));
    const types = screen.getAllByTestId("result-row").map((tr) => tr.getAttribute("data-mod-type"));
    expect(types[0]).toBe("Cm");
    expect(types[types.length - 1]).toBe("Psi");
  });

  it("downloads the server CSV through the injected source and names the file from it", async () => {
    const user = userEvent.setup();
    const blob = new Blob(["x"], { type: "text/csv" });
    let resolve!: (b: Blob) => void;
    const csv = makeCsv();
    csv.download.mockReturnValue(new Promise<Blob>((r) => (resolve = r)));
    renderTable({ csv });
    const button = screen.getByTestId("download-csv");
    await user.click(button);
    expect(csv.download).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    resolve(blob);
    await waitFor(() => expect(button).toBeEnabled());
    expect(downloadBlob).toHaveBeenCalledWith(blob, "rmodhub_sites_sequence_151nt.csv");
    expect(screen.queryByTestId("download-csv-error")).not.toBeInTheDocument();
  });

  it("shows a readable error when the server CSV fails", async () => {
    const user = userEvent.setup();
    const csv = makeCsv();
    csv.download.mockRejectedValue(new TypeError("Failed to fetch"));
    renderTable({ csv });
    await user.click(screen.getByTestId("download-csv"));
    expect(await screen.findByTestId("download-csv-error")).toHaveTextContent("Cannot reach the RModHub server");
    expect(downloadBlob).not.toHaveBeenCalled();
    expect(screen.getByTestId("download-csv")).toBeEnabled();
  });

  it("downloads the visible rows as a client-side CSV", async () => {
    const user = userEvent.setup();
    renderTable();
    await user.click(screen.getByTestId("filter-mod-type-none"));
    await user.click(screen.getByTestId("filter-mod-type-m5C"));
    await user.click(screen.getByTestId("download-visible-csv"));
    expect(downloadText).toHaveBeenCalledTimes(1);
    const [text, name, mime] = vi.mocked(downloadText).mock.calls[0];
    expect(name).toBe("rmodhub_sites_sequence_151nt_filtered.csv");
    expect(mime).toBe("text/csv");
    const lines = text.trimEnd().split("\n");
    expect(lines[0]).toBe("transcript_id,position,mod_type,probability,p_value,coverage,source");
    expect(lines).toHaveLength(7);
  });
});

describe("ResultsTable (894-row fixture)", () => {
  it("renders only the current page and paginates", async () => {
    const user = userEvent.setup();
    renderTable({ sites: many, meta: manyMeta });
    expect(screen.getAllByTestId("result-row")).toHaveLength(50);
    expect(screen.getByTestId("visible-count")).toHaveTextContent("Showing 894 of 894 sites");
    expect(screen.getByTestId("page-info")).toHaveTextContent("Page 1 of 18");
    expect(screen.getByTestId("page-prev")).toBeDisabled();

    await user.click(screen.getByTestId("page-next"));
    expect(screen.getByTestId("page-info")).toHaveTextContent("Page 2 of 18");
    expect(rowKeys()[0]).toBe(siteKey(many[50]));
    // The # column continues across pages.
    expect(within(screen.getAllByTestId("result-row")[0]).getAllByRole("cell")[0]).toHaveTextContent("51");

    await user.selectOptions(screen.getByTestId("page-size"), "250");
    expect(screen.getByTestId("page-info")).toHaveTextContent("Page 1 of 4");
    expect(screen.getAllByTestId("result-row")).toHaveLength(250);

    await user.selectOptions(screen.getByTestId("page-size"), "25");
    for (let i = 0; i < 35; i++) await user.click(screen.getByTestId("page-next"));
    expect(screen.getByTestId("page-info")).toHaveTextContent("Page 36 of 36");
    expect(screen.getByTestId("page-next")).toBeDisabled();
    expect(screen.getAllByTestId("result-row")).toHaveLength(894 - 35 * 25);

    // Changing a filter or the sort goes back to page 1.
    await user.click(screen.getByTestId("sort-probability"));
    expect(screen.getByTestId("page-info")).toHaveTextContent("Page 1 of 36");
  });

  it("an external selection jumps to the page holding the row and scrolls it into view", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    const target = siteKey(many[120]); // page 3 at 50 rows/page
    const { rerender, onSelect, csv } = renderTable({ sites: many, meta: manyMeta });
    expect(screen.getByTestId("page-info")).toHaveTextContent("Page 1 of 18");
    rerender(<ResultsTable sites={many} meta={manyMeta} csv={csv} selectedKey={target} onSelect={onSelect} />);
    await waitFor(() => expect(screen.getByTestId("page-info")).toHaveTextContent("Page 3 of 18"));
    const selected = screen.getAllByTestId("result-row").find((tr) => tr.getAttribute("aria-selected") === "true");
    expect(selected).toHaveAttribute("data-key", target);
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" }));
  });
});

describe("ResultsTable (signal rows)", () => {
  const allSites = signalResults.results as SignalSite[];
  const txA = allSites.filter((s) => s.transcript_id === "tx_A");
  const txMeta = transcriptMeta(
    signalResults.meta as never,
    signalResults.meta.transcripts[0],
    txA.length,
  );
  const csv: CsvSource = { download: vi.fn(), filename: "rmodhub_signal_job_sites.csv" };

  it("adds the transcript, strand, CI, coverage and count columns and drops p-value", () => {
    render(<ResultsTable sites={txA} meta={txMeta} csv={csv} selectedKey={null} onSelect={vi.fn()} />);
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toEqual([
      "#", "Transcript", "Position", "Strand", "Modification", "Probability", "95% CI", "Coverage", "Modified reads",
    ]);
    expect(headers).not.toContain("p-value");
    const first = screen.getAllByTestId("result-row")[0];
    const cells = within(first).getAllByRole("cell").map((td) => td.textContent);
    const site = txA[0];
    expect(cells).toEqual([
      "1",
      "tx_A",
      String(site.position),
      site.strand,
      expect.stringContaining(site.mod_type === "Psi" ? "Psi" : site.mod_type),
      site.probability.toFixed(3),
      `[${site.ci_low.toFixed(3)}, ${site.ci_high.toFixed(3)}]`,
      String(site.coverage),
      String(site.count),
    ]);
    // ac4C (outside the frozen 12) gets its own filter chip with a count.
    expect(screen.getByTestId("filter-mod-type-ac4C")).toHaveTextContent(
      String(txA.filter((s) => s.mod_type === "ac4C").length),
    );
    // Every row passes the default filters (alpha 1, positions 1..length).
    expect(screen.getByTestId("visible-count")).toHaveTextContent(`Showing ${txA.length} of ${txA.length} sites`);
  });

  it("offers only the types DirectRM calls as chips (canonical order), no p-value box, 'Rate ≥'", () => {
    render(<ResultsTable sites={txA} meta={txMeta} csv={csv} selectedKey={null} onSelect={vi.fn()} />);
    const toolbar = screen.getByTestId("results-toolbar");
    const chips = Array.from(toolbar.querySelectorAll('[data-testid^="filter-mod-type-"]'))
      .map((el) => el.getAttribute("data-testid")!.replace("filter-mod-type-", ""))
      .filter((id) => id !== "all" && id !== "none");
    expect(chips).toEqual(["m1A", "m5C", "m6A", "m7G", "Psi", "ac4C"]);
    expect(toolbar).toHaveTextContent("(6/6 selected)");
    expect(screen.queryByTestId("filter-pvalue-max")).not.toBeInTheDocument();
    expect(toolbar).toHaveTextContent("Rate ≥");
    expect(toolbar).not.toHaveTextContent("Probability ≥");
    // Rows of signal results are keyed with their strand.
    expect(screen.getAllByTestId("result-row")[0]).toHaveAttribute("data-key", `${txA[0].position}:${txA[0].mod_type}:+`);
  });

  it("labels the server CSV with the job-wide row count when the table shows one transcript", () => {
    render(<ResultsTable sites={txA} meta={txMeta} csv={{ ...csv, totalRows: 22 }} selectedKey={null} onSelect={vi.fn()} />);
    expect(screen.getByTestId("download-csv")).toHaveTextContent("Download CSV (all 22 sites)");
    expect(screen.getByTestId("download-csv-note")).toHaveTextContent(/all 22 sites of every transcript/);
    expect(screen.getByTestId("visible-count")).toHaveTextContent(`Showing ${txA.length} of ${txA.length} sites`);
  });

  it("sorts by coverage and by count", async () => {
    const user = userEvent.setup();
    render(<ResultsTable sites={txA} meta={txMeta} csv={csv} selectedKey={null} onSelect={vi.fn()} />);
    await user.click(screen.getByTestId("sort-coverage"));
    const covs = screen.getAllByTestId("result-row").map((tr) => Number(within(tr).getAllByRole("cell")[7].textContent));
    for (let i = 1; i < covs.length; i++) expect(covs[i]).toBeGreaterThanOrEqual(covs[i - 1]);
    await user.click(screen.getByTestId("sort-count"));
    await user.click(screen.getByTestId("sort-count"));
    const counts = screen.getAllByTestId("result-row").map((tr) => Number(within(tr).getAllByRole("cell")[8].textContent));
    expect(counts[0]).toBe(Math.max(...txA.map((s) => s.count)));
  });

  it("the client-side CSV of signal rows carries the server's extra columns", async () => {
    const user = userEvent.setup();
    render(<ResultsTable sites={txA} meta={txMeta} csv={csv} selectedKey={null} onSelect={vi.fn()} />);
    await user.click(screen.getByTestId("download-visible-csv"));
    const [text, name] = vi.mocked(downloadText).mock.calls.at(-1)!;
    expect(name).toBe("rmodhub_signal_job_sites_filtered.csv");
    const lines = text.trimEnd().split("\n");
    expect(lines[0]).toBe(
      "transcript_id,position,mod_type,probability,p_value,coverage,source,strand,count,ci_low,ci_high,max_prob,noisyor_prob",
    );
    expect(lines).toHaveLength(txA.length + 1);
    expect(lines[1].startsWith(`tx_A,${txA[0].position},${txA[0].mod_type},${txA[0].probability},,${txA[0].coverage},signal,+,`)).toBe(true);
  });
});

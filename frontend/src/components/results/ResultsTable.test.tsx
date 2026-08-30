import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { siteKey, type ModSite, type PredictionMeta, type PredictRequest } from "../../api/types";
import golden from "../../api/fixtures/golden_attention.json";
import manyRows from "../../api/fixtures/many_rows.json";
import { ResultsTable } from "./ResultsTable";

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return { ...actual, predictSequenceCsv: vi.fn() };
});
vi.mock("../../lib/download", () => ({ downloadBlob: vi.fn(), downloadText: vi.fn() }));

import { predictSequenceCsv } from "../../api/client";
import { downloadBlob, downloadText } from "../../lib/download";

const rows = golden.results as ModSite[];
const meta = golden.meta as unknown as PredictionMeta;
const request: PredictRequest = { sequence: "A".repeat(151), alpha: 0.05, include_attention: true };
const many = manyRows.response.results as ModSite[];
const manyMeta = manyRows.response.meta as unknown as PredictionMeta;

function renderTable(props: Partial<React.ComponentProps<typeof ResultsTable>> = {}) {
  const onSelect = vi.fn();
  const onVisibleChange = vi.fn();
  const utils = render(
    <ResultsTable
      sites={rows}
      meta={meta}
      request={request}
      selectedKey={null}
      onSelect={onSelect}
      onVisibleChange={onVisibleChange}
      {...props}
    />,
  );
  return { ...utils, onSelect, onVisibleChange };
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
    // Sequence branch: no transcript / coverage columns.
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toEqual(["#", "Position", "Modification", "Probability", "p-value"]);
    // The page is told which rows are visible (all of them, in table order).
    expect(onVisibleChange).toHaveBeenCalledTimes(1);
    expect(onVisibleChange.mock.calls[0][0].map(siteKey)).toEqual(rows.map(siteKey));
  });

  it("clicking a row selects it; clicking the selected row again clears the selection", async () => {
    const user = userEvent.setup();
    const { onSelect, rerender } = renderTable();
    await user.click(screen.getAllByTestId("result-row")[0]);
    expect(onSelect).toHaveBeenCalledWith("52:Gm");

    rerender(
      <ResultsTable sites={rows} meta={meta} request={request} selectedKey="52:Gm" onSelect={onSelect} />,
    );
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

  it("downloads the server CSV with the request and the expected filename", async () => {
    const user = userEvent.setup();
    const blob = new Blob(["x"], { type: "text/csv" });
    let resolve!: (b: Blob) => void;
    vi.mocked(predictSequenceCsv).mockReturnValue(new Promise<Blob>((r) => (resolve = r)));
    renderTable();
    const button = screen.getByTestId("download-csv");
    await user.click(button);
    expect(predictSequenceCsv).toHaveBeenCalledTimes(1);
    expect(vi.mocked(predictSequenceCsv).mock.calls[0][0]).toBe(request);
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    resolve(blob);
    await waitFor(() => expect(button).toBeEnabled());
    expect(downloadBlob).toHaveBeenCalledWith(blob, "rmodhub_sites_sequence_151nt.csv");
    expect(screen.queryByTestId("download-csv-error")).not.toBeInTheDocument();
  });

  it("shows a readable error when the server CSV fails", async () => {
    const user = userEvent.setup();
    vi.mocked(predictSequenceCsv).mockRejectedValue(new TypeError("Failed to fetch"));
    renderTable();
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
    const { rerender, onSelect } = renderTable({ sites: many, meta: manyMeta });
    expect(screen.getByTestId("page-info")).toHaveTextContent("Page 1 of 18");
    rerender(
      <ResultsTable sites={many} meta={manyMeta} request={request} selectedKey={target} onSelect={onSelect} />,
    );
    await waitFor(() => expect(screen.getByTestId("page-info")).toHaveTextContent("Page 3 of 18"));
    const selected = screen.getAllByTestId("result-row").find((tr) => tr.getAttribute("aria-selected") === "true");
    expect(selected).toHaveAttribute("data-key", target);
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" }));
  });
});

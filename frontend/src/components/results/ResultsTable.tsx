/**
 * Results table for the shared `ModSite` rows: filter toolbar, sortable columns,
 * pagination (only the current page is in the DOM), row selection shared with the track
 * view, and CSV download (server `?format=csv` for all rows, client-side for the filtered
 * rows). All pure logic lives in ./resultsModel.ts.
 *
 * The props are the contract with SequencePage (and the future signal-branch result page).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { describeError, predictSequenceCsv } from "../../api/client";
import { siteKey, type ModSite, type PredictionMeta, type PredictRequest } from "../../api/types";
import { downloadBlob, downloadText } from "../../lib/download";
import { ModTypeBadge } from "./ModTypeBadge";
import { ResultsPagination } from "./ResultsPagination";
import { ResultsToolbar } from "./ResultsToolbar";
import {
  allModTypes,
  csvFilename,
  DEFAULT_PAGE_SIZE,
  DEFAULT_SORT,
  defaultFilterInputs,
  filterSites,
  modTypeCounts,
  pageOf,
  paginate,
  sortSites,
  toCsv,
  toFilters,
  visibleColumns,
  type ColumnDef,
  type FilterInputs,
  type SortKey,
  type SortState,
} from "./resultsModel";

export interface ResultsTableProps {
  /** All rows returned by the API (unfiltered). */
  sites: ModSite[];
  meta: PredictionMeta;
  /** Exact request body that produced `sites`; reuse it for `?format=csv`. */
  request: PredictRequest;
  /** Shared selection with the track view: siteKey(site) or null. */
  selectedKey: string | null;
  onSelect: (key: string | null) => void;
  /** Rows currently passing the table's filters (all pages), so the track view matches. */
  onVisibleChange?: (visible: ModSite[]) => void;
}

export function ResultsTable({ sites, meta, request, selectedKey, onSelect, onVisibleChange }: ResultsTableProps) {
  const [inputs, setInputs] = useState<FilterInputs>(() => defaultFilterInputs(meta, sites));
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  const [page, setPage] = useState(1);
  /** Selection made elsewhere (track view) that still has to be brought on screen. */
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [csvBusy, setCsvBusy] = useState(false);
  const [csvError, setCsvError] = useState<string | null>(null);

  // A new result set resets filters, sort and page (state adjusted during render, per React docs).
  const [prevSites, setPrevSites] = useState(sites);
  if (prevSites !== sites) {
    setPrevSites(sites);
    setInputs(defaultFilterInputs(meta, sites));
    setSort(DEFAULT_SORT);
    setPage(1);
    setCsvError(null);
  }
  // Remember an external selection change until the row is on the current page.
  const [prevSelected, setPrevSelected] = useState(selectedKey);
  if (prevSelected !== selectedKey) {
    setPrevSelected(selectedKey);
    setPendingKey(selectedKey);
  }

  const filters = useMemo(() => toFilters(inputs), [inputs]);
  const filtered = useMemo(() => filterSites(sites, filters), [sites, filters]);
  const sorted = useMemo(() => sortSites(filtered, sort), [filtered, sort]);
  const columns = useMemo(() => visibleColumns(sites), [sites]);
  const modTypes = useMemo(() => allModTypes(sites), [sites]);
  const counts = useMemo(() => modTypeCounts(sites), [sites]);
  const pageData = paginate(sorted, page, pageSize);

  // Tell the page which rows pass the filters — only when the list really changed.
  const lastEmitted = useRef<string | null>(null);
  useEffect(() => {
    if (!onVisibleChange) return;
    const signature = sorted.map(siteKey).join("|");
    if (signature === lastEmitted.current) return;
    lastEmitted.current = signature;
    onVisibleChange(sorted);
  }, [sorted, onVisibleChange]);

  // Reveal the selected row: jump to its page, then scroll it into view.
  const rowRefs = useRef(new Map<string, HTMLTableRowElement>());
  useEffect(() => {
    if (pendingKey === null) return;
    const index = sorted.findIndex((s) => siteKey(s) === pendingKey);
    if (index === -1) {
      setPendingKey(null); // filtered out: nothing to reveal
      return;
    }
    const target = pageOf(index, pageSize);
    if (target !== pageData.page) {
      setPage(target); // effect re-runs once the page has rendered
      return;
    }
    const row = rowRefs.current.get(pendingKey);
    if (row && typeof row.scrollIntoView === "function") row.scrollIntoView({ block: "nearest" });
    setPendingKey(null);
  }, [pendingKey, sorted, pageSize, pageData.page]);

  const updateInputs = useCallback((patch: Partial<FilterInputs>) => {
    setInputs((prev) => ({ ...prev, ...patch }));
    setPage(1);
  }, []);
  const resetFilters = useCallback(() => {
    setInputs(defaultFilterInputs(meta, sites));
    setPage(1);
  }, [meta, sites]);
  const toggleSort = (key: SortKey) => {
    setSort((prev) => (prev.key === key ? { key, dir: prev.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
    setPage(1);
  };
  const changePageSize = (size: number) => {
    setPageSize(size);
    setPage(1);
  };
  const selectRow = (key: string) => onSelect(key === selectedKey ? null : key);

  const downloadServerCsv = async () => {
    setCsvBusy(true);
    setCsvError(null);
    try {
      const blob = await predictSequenceCsv(request);
      downloadBlob(blob, csvFilename(meta));
    } catch (err) {
      setCsvError(describeError(err));
    } finally {
      setCsvBusy(false);
    }
  };
  const downloadVisibleCsv = () => downloadText(toCsv(sorted), csvFilename(meta, "filtered"), "text/csv");

  return (
    <section data-testid="results-table-section" className="space-y-3">
      <ResultsToolbar
        inputs={inputs}
        onChange={updateInputs}
        onReset={resetFilters}
        modTypes={modTypes}
        counts={counts}
        meta={meta}
      />

      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 text-sm">
        <p data-testid="visible-count" className="font-medium text-slate-700" aria-live="polite">
          Showing {sorted.length} of {sites.length} sites
        </p>
        <div className="flex flex-col items-end gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="download-csv"
              onClick={downloadServerCsv}
              disabled={csvBusy}
              aria-busy={csvBusy}
              className="inline-flex items-center gap-2 rounded bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50"
            >
              {csvBusy ? (
                <span
                  aria-hidden
                  className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-white border-t-transparent"
                />
              ) : (
                <DownloadIcon />
              )}
              {csvBusy ? "Preparing CSV…" : `Download CSV (all ${sites.length} sites)`}
            </button>
            <button
              type="button"
              data-testid="download-visible-csv"
              onClick={downloadVisibleCsv}
              disabled={sorted.length === 0}
              className="inline-flex items-center gap-2 rounded border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <DownloadIcon />
              Download visible rows ({sorted.length})
            </button>
          </div>
          <p className="text-xs text-slate-500">
            The server CSV contains all {sites.length} sites regardless of the table filters.
          </p>
          {csvError && (
            <p data-testid="download-csv-error" role="alert" className="text-xs text-red-700">
              Download failed: {csvError}
            </p>
          )}
        </div>
      </div>

      {sorted.length === 0 ? (
        <div
          data-testid="results-empty"
          className="rounded border border-slate-200 bg-white px-4 py-6 text-center text-sm text-slate-600"
        >
          <p>No sites match the current filters</p>
          <button
            type="button"
            data-testid="filter-reset-empty"
            onClick={resetFilters}
            className="mt-2 rounded border border-slate-300 bg-white px-3 py-1 text-sm hover:bg-slate-50"
          >
            Reset filters
          </button>
        </div>
      ) : (
        <>
          <div className="overflow-x-auto rounded border border-slate-200 bg-white">
            <table data-testid="results-table" className="w-full min-w-[36rem] border-collapse text-sm">
              <caption className="sr-only">
                Predicted modification sites, {sorted.length} of {sites.length} shown, page {pageData.page} of{" "}
                {pageData.pageCount}
              </caption>
              <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
                <tr>
                  {columns.map((col) => (
                    <HeaderCell key={col.id} col={col} sort={sort} onSort={toggleSort} />
                  ))}
                </tr>
              </thead>
              <tbody>
                {pageData.items.map((site, i) => {
                  const key = siteKey(site);
                  const selected = key === selectedKey;
                  return (
                    <tr
                      key={key}
                      ref={(el) => {
                        if (el) rowRefs.current.set(key, el);
                        else rowRefs.current.delete(key);
                      }}
                      data-testid="result-row"
                      data-key={key}
                      data-position={site.position}
                      data-mod-type={site.mod_type}
                      aria-selected={selected}
                      tabIndex={0}
                      onClick={() => selectRow(key)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          selectRow(key);
                        }
                      }}
                      className={`cursor-pointer border-t border-slate-100 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-600 ${
                        selected ? "bg-brand-100 hover:bg-brand-100" : "odd:bg-white even:bg-slate-50/60 hover:bg-brand-50"
                      }`}
                    >
                      {columns.map((col) => (
                        <td
                          key={col.id}
                          className={`px-3 py-1.5 ${col.align === "right" ? "text-right tabular-nums" : "text-left"} ${
                            col.id === "index" ? "text-slate-400" : ""
                          }`}
                        >
                          {col.id === "index" ? (
                            pageData.start + i + 1
                          ) : col.id === "mod_type" ? (
                            <ModTypeBadge id={site.mod_type} />
                          ) : (
                            col.format(site)
                          )}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <ResultsPagination
            page={pageData.page}
            pageCount={pageData.pageCount}
            pageSize={pageData.pageSize}
            start={pageData.start}
            shown={pageData.items.length}
            total={pageData.total}
            onPageChange={setPage}
            onPageSizeChange={changePageSize}
          />
        </>
      )}
    </section>
  );
}

function HeaderCell({ col, sort, onSort }: { col: ColumnDef; sort: SortState; onSort: (key: SortKey) => void }) {
  const alignClass = col.align === "right" ? "text-right" : "text-left";
  if (!col.sortable) {
    return (
      <th scope="col" title={col.title} className={`px-3 py-2 font-semibold ${alignClass}`}>
        {col.label}
      </th>
    );
  }
  const key = col.id as SortKey;
  const active = sort.key === key;
  const ariaSort = active ? (sort.dir === "asc" ? "ascending" : "descending") : "none";
  return (
    <th scope="col" aria-sort={ariaSort} className={`px-0 py-0 font-semibold ${alignClass}`}>
      <button
        type="button"
        data-testid={`sort-${key}`}
        onClick={() => onSort(key)}
        title={`${col.title}. Click to sort ${active && sort.dir === "asc" ? "descending" : "ascending"}.`}
        className={`inline-flex w-full items-center gap-1 px-3 py-2 uppercase hover:bg-slate-100 ${
          col.align === "right" ? "justify-end" : "justify-start"
        } ${active ? "text-brand-800" : ""}`}
      >
        {col.label}
        <SortArrow state={active ? sort.dir : "none"} />
      </button>
    </th>
  );
}

function SortArrow({ state }: { state: "none" | "asc" | "desc" }) {
  return (
    <svg
      viewBox="0 0 12 12"
      width="10"
      height="10"
      aria-hidden="true"
      className={`shrink-0 ${state === "none" ? "text-slate-300" : "text-brand-600"}`}
    >
      {state === "asc" && <path d="M2 8l4-5 4 5z" fill="currentColor" />}
      {state === "desc" && <path d="M2 4l4 5 4-5z" fill="currentColor" />}
      {state === "none" && (
        <>
          <path d="M2.5 5l3.5-4 3.5 4z" fill="currentColor" />
          <path d="M2.5 7l3.5 4 3.5-4z" fill="currentColor" />
        </>
      )}
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" className="shrink-0">
      <path
        d="M8 2v8m0 0l-3-3m3 3l3-3M3 12v1.5h10V12"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Page-size selector + prev/next + "Page i of n". Controlled by the table. */
import { useId } from "react";
import { PAGE_SIZES } from "./resultsModel";

export interface ResultsPaginationProps {
  page: number;
  pageCount: number;
  pageSize: number;
  /** 0-based index of the first row on the page and the total row count (for "rows a–b"). */
  start: number;
  shown: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}

const navButtonClass =
  "rounded border border-slate-300 bg-white px-2.5 py-1 text-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40";

export function ResultsPagination(p: ResultsPaginationProps) {
  const sizeId = useId();
  const first = p.total === 0 ? 0 : p.start + 1;
  const last = p.start + p.shown;
  return (
    <nav aria-label="Table pagination" className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-sm">
      <div className="flex items-center gap-2">
        <label htmlFor={sizeId} className="text-slate-600">
          Rows per page
        </label>
        <select
          id={sizeId}
          data-testid="page-size"
          value={p.pageSize}
          onChange={(e) => p.onPageSizeChange(Number(e.target.value))}
          className="rounded border border-slate-300 bg-white px-1.5 py-1 text-sm focus:border-brand-600 focus:outline-none"
        >
          {PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
        <span className="text-slate-500">
          rows {first}–{last} of {p.total}
        </span>
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="page-prev"
          onClick={() => p.onPageChange(p.page - 1)}
          disabled={p.page <= 1}
          className={navButtonClass}
        >
          <svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true" className="mr-1 inline-block align-[-1px]">
            <path d="M8 1L3 6l5 5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Previous
        </button>
        <span data-testid="page-info" aria-live="polite" className="tabular-nums text-slate-700">
          Page {p.page} of {p.pageCount}
        </span>
        <button
          type="button"
          data-testid="page-next"
          onClick={() => p.onPageChange(p.page + 1)}
          disabled={p.page >= p.pageCount}
          className={navButtonClass}
        >
          Next
          <svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true" className="ml-1 inline-block align-[-1px]">
            <path d="M4 1l5 5-5 5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
    </nav>
  );
}

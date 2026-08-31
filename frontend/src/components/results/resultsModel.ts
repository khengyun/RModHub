/**
 * Pure logic behind `ResultsTable`: column model, filtering, sorting, pagination and CSV
 * export. No React in here so everything can be unit-tested against the API fixtures.
 *
 * Rows are `ModSite` (the frozen cross-branch schema). A row is identified by
 * `siteKey(site)` = "position:mod_type"; the same position can appear with several
 * modification types and those rows are never merged.
 */
import { isSignalSite, MOD_TYPES, siteKey, type ModSite, type PredictionMeta } from "../../api/types";
import { formatP, formatProb } from "../../lib/format";
import { modTypeInfo } from "../../lib/modTypes";

/* ----------------------------------------------------------------------------------------
 * Columns
 * -------------------------------------------------------------------------------------- */

export type SortKey =
  | "position"
  | "mod_type"
  | "probability"
  | "p_value"
  | "transcript_id"
  | "coverage"
  // signal-branch extras (docs/signal-branch.md section 6)
  | "strand"
  | "count"
  | "ci_low";
export type ColumnId = "index" | SortKey;

export interface ColumnDef {
  id: ColumnId;
  label: string;
  /** Tooltip on the header. */
  title: string;
  align: "left" | "right";
  sortable: boolean;
  /** Cell text (the component renders `mod_type` as a badge and `index` from the row number). */
  format: (site: ModSite) => string;
  /**
   * When set, the column only appears if at least one row satisfies it. Used for the
   * signal-branch columns (transcript, strand, coverage, count, CI) that are absent or
   * null for the sequence branch, and for p-value, which the signal branch never has.
   */
  visibleWhen?: (sites: readonly ModSite[]) => boolean;
}

const anySignal = (sites: readonly ModSite[]) => sites.some(isSignalSite);

/** "[0.12, 0.45]" for a signal row, "—" otherwise. */
export function formatCi(site: ModSite): string {
  return isSignalSite(site) ? `[${formatProb(site.ci_low)}, ${formatProb(site.ci_high)}]` : "—";
}

export const COLUMNS: readonly ColumnDef[] = [
  {
    id: "index",
    label: "#",
    title: "Row number in the current (filtered, sorted) list",
    align: "right",
    sortable: false,
    format: () => "",
  },
  {
    id: "transcript_id",
    label: "Transcript",
    title: "Transcript identifier (from the FASTA header or the alignment)",
    align: "left",
    sortable: true,
    format: (s) => s.transcript_id ?? "—",
    visibleWhen: (sites) => sites.some((s) => s.transcript_id !== null),
  },
  {
    id: "position",
    label: "Position",
    title: "1-based position in the sequence as entered",
    align: "right",
    sortable: true,
    format: (s) => String(s.position),
  },
  {
    id: "strand",
    label: "Strand",
    title: "Strand of the region the site was called on (signal branch only)",
    align: "left",
    sortable: true,
    format: (s) => (isSignalSite(s) ? s.strand : "—"),
    visibleWhen: anySignal,
  },
  {
    id: "mod_type",
    label: "Modification",
    title: "Predicted modification type (sorted in the model's canonical order)",
    align: "left",
    sortable: true,
    format: (s) => modTypeInfo(s.mod_type).label,
  },
  {
    id: "probability",
    label: "Probability",
    title:
      "Sequence branch: model probability that the site carries this modification. Signal branch: modification rate = modified reads / coverage",
    align: "right",
    sortable: true,
    format: (s) => formatProb(s.probability),
  },
  {
    id: "ci_low",
    label: "95% CI",
    title: "95 % Wilson score interval of the modification rate (signal branch only); sorted by its lower bound",
    align: "right",
    sortable: true,
    format: formatCi,
    visibleWhen: anySignal,
  },
  {
    id: "p_value",
    label: "p-value",
    title: "Empirical p-value (MultiRM: multiples of 1/150)",
    align: "right",
    sortable: true,
    format: (s) => formatP(s.p_value),
    visibleWhen: (sites) => sites.length === 0 || sites.some((s) => s.p_value !== null),
  },
  {
    id: "coverage",
    label: "Coverage",
    title: "Number of reads covering the site (signal branch only)",
    align: "right",
    sortable: true,
    format: (s) => (s.coverage === null ? "—" : String(s.coverage)),
    visibleWhen: (sites) => sites.some((s) => s.coverage !== null),
  },
  {
    id: "count",
    label: "Modified reads",
    title: "Reads with a per-read probability above 0.5 at this site (signal branch only)",
    align: "right",
    sortable: true,
    format: (s) => (isSignalSite(s) ? String(s.count) : "—"),
    visibleWhen: anySignal,
  },
];

/** Columns to render for this result set (data-driven: optional columns need a value). */
export function visibleColumns(sites: readonly ModSite[]): ColumnDef[] {
  return COLUMNS.filter((c) => c.visibleWhen === undefined || c.visibleWhen(sites));
}

/* ----------------------------------------------------------------------------------------
 * Sorting
 * -------------------------------------------------------------------------------------- */

export type SortDir = "asc" | "desc";
export interface SortState {
  key: SortKey;
  dir: SortDir;
}

export const DEFAULT_SORT: SortState = { key: "position", dir: "asc" };

/** Position of a modification type in the canonical order; unknown types sort last. */
export function modTypeRank(modType: string): number {
  const i = (MOD_TYPES as readonly string[]).indexOf(modType);
  return i === -1 ? MOD_TYPES.length : i;
}

function sortValue(site: ModSite, key: SortKey): number | string | null {
  switch (key) {
    case "position":
      return site.position;
    case "mod_type":
      return modTypeRank(site.mod_type);
    case "probability":
      return site.probability;
    case "p_value":
      return site.p_value;
    case "transcript_id":
      return site.transcript_id;
    case "coverage":
      return site.coverage;
    case "strand":
      return isSignalSite(site) ? site.strand : null;
    case "count":
      return isSignalSite(site) ? site.count : null;
    case "ci_low":
      return isSignalSite(site) ? site.ci_low : null;
  }
}

function compareValues(a: number | string | null, b: number | string | null): number {
  if (a === null || b === null) {
    // Nulls last, whatever the direction.
    return a === null && b === null ? 0 : a === null ? 1 : -1;
  }
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), "en");
}

/** Canonical order: position ascending, then MOD_TYPES order (unknown types by name). */
export function compareCanonical(a: ModSite, b: ModSite): number {
  return (
    a.position - b.position ||
    modTypeRank(a.mod_type) - modTypeRank(b.mod_type) ||
    a.mod_type.localeCompare(b.mod_type, "en")
  );
}

/** Return a sorted copy. Nulls always go last; ties fall back to the canonical order. */
export function sortSites(sites: readonly ModSite[], sort: SortState): ModSite[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  return [...sites].sort((a, b) => {
    const va = sortValue(a, sort.key);
    const vb = sortValue(b, sort.key);
    let c: number;
    if (va === null || vb === null) c = compareValues(va, vb); // nulls last regardless of dir
    else c = sign * compareValues(va, vb);
    return c || compareCanonical(a, b);
  });
}

/* ----------------------------------------------------------------------------------------
 * Filtering
 * -------------------------------------------------------------------------------------- */

/** Parsed filters, as applied to the rows. */
export interface Filters {
  /** Modification types to keep (a row passes when its mod_type is in the set). */
  modTypes: ReadonlySet<string>;
  /** Keep rows with p_value <= pMax (null p-values always pass). null = no limit. */
  pMax: number | null;
  /** Keep rows with probability >= probMin. null = no limit. */
  probMin: number | null;
  posMin: number | null;
  posMax: number | null;
  /** Quick filter: whitespace-separated tokens, all must match (see `matchesText`). */
  text: string;
}

/** What the toolbar inputs hold (strings, so partially typed numbers are not clobbered). */
export interface FilterInputs {
  modTypes: ReadonlySet<string>;
  pMax: string;
  probMin: string;
  posMin: string;
  posMax: string;
  text: string;
}

/** All 12 canonical types plus any unexpected type present in the rows (sorted by name). */
export function allModTypes(sites: readonly ModSite[]): string[] {
  const extra = new Set<string>();
  for (const s of sites) if (modTypeRank(s.mod_type) === MOD_TYPES.length) extra.add(s.mod_type);
  return [...MOD_TYPES, ...[...extra].sort((a, b) => a.localeCompare(b, "en"))];
}

/**
 * Chips of the filter toolbar: the types the model can call (`meta.mod_types` for the
 * signal branch, where DirectRM knows six; the 12 canonical types for the sequence branch)
 * plus any other type present in the rows, in canonical order (unknown types last, by name).
 */
export function chipModTypes(meta: Pick<PredictionMeta, "source" | "mod_types">, sites: readonly ModSite[]): string[] {
  if (meta.source !== "signal" || !Array.isArray(meta.mod_types) || meta.mod_types.length === 0) {
    return allModTypes(sites);
  }
  const set = new Set<string>(meta.mod_types);
  for (const s of sites) set.add(s.mod_type);
  return [...set].sort((a, b) => modTypeRank(a) - modTypeRank(b) || a.localeCompare(b, "en"));
}

/** Number of rows per modification type (over the unfiltered rows). */
export function modTypeCounts(sites: readonly ModSite[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const s of sites) counts.set(s.mod_type, (counts.get(s.mod_type) ?? 0) + 1);
  return counts;
}

export function defaultFilterInputs(meta: PredictionMeta, sites: readonly ModSite[]): FilterInputs {
  return {
    modTypes: new Set(chipModTypes(meta, sites)),
    pMax: String(meta.alpha),
    probMin: "0",
    posMin: String(meta.predicted_start),
    posMax: String(meta.predicted_end),
    text: "",
  };
}

/** "" / non-numeric -> null (no constraint). */
export function parseNumber(text: string): number | null {
  const t = text.trim();
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

export function toFilters(inputs: FilterInputs): Filters {
  return {
    modTypes: inputs.modTypes,
    pMax: parseNumber(inputs.pMax),
    probMin: parseNumber(inputs.probMin),
    posMin: parseNumber(inputs.posMin),
    posMax: parseNumber(inputs.posMax),
    text: inputs.text,
  };
}

/**
 * Quick-filter match. Tokens are separated by whitespace and all must match. A purely
 * numeric token matches the position exactly; any other token is a case-insensitive
 * substring of the modification id / label, the "position:type" key or the transcript id.
 */
export function matchesText(site: ModSite, text: string): boolean {
  const tokens = text.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return true;
  const haystack = [
    site.mod_type,
    modTypeInfo(site.mod_type).label,
    siteKey(site),
    site.transcript_id ?? "",
  ].map((s) => s.toLowerCase());
  return tokens.every((tok) =>
    /^\d+$/.test(tok) ? site.position === Number(tok) : haystack.some((h) => h.includes(tok)),
  );
}

export function filterSites(sites: readonly ModSite[], f: Filters): ModSite[] {
  return sites.filter(
    (s) =>
      f.modTypes.has(s.mod_type) &&
      (f.pMax === null || s.p_value === null || s.p_value <= f.pMax) &&
      (f.probMin === null || s.probability >= f.probMin) &&
      (f.posMin === null || s.position >= f.posMin) &&
      (f.posMax === null || s.position <= f.posMax) &&
      matchesText(s, f.text),
  );
}

/* ----------------------------------------------------------------------------------------
 * Pagination
 * -------------------------------------------------------------------------------------- */

export const PAGE_SIZES = [25, 50, 100, 250] as const;
export const DEFAULT_PAGE_SIZE = 50;

export interface Page<T> {
  items: T[];
  /** 1-based, clamped to [1, pageCount]. */
  page: number;
  pageCount: number;
  pageSize: number;
  /** 0-based index (into the full list) of the first item on this page. */
  start: number;
  total: number;
}

export function paginate<T>(items: readonly T[], page: number, pageSize: number): Page<T> {
  const size = Math.max(1, Math.floor(pageSize));
  const pageCount = Math.max(1, Math.ceil(items.length / size));
  const current = Math.min(Math.max(1, Math.floor(page) || 1), pageCount);
  const start = (current - 1) * size;
  return {
    items: items.slice(start, start + size),
    page: current,
    pageCount,
    pageSize: size,
    start,
    total: items.length,
  };
}

/** 1-based page that contains the item at 0-based `index`. */
export function pageOf(index: number, pageSize: number): number {
  return Math.floor(index / Math.max(1, pageSize)) + 1;
}

/* ----------------------------------------------------------------------------------------
 * CSV
 * -------------------------------------------------------------------------------------- */

/** Same header as the backend's `?format=csv` output. */
export const CSV_HEADER = [
  "transcript_id",
  "position",
  "mod_type",
  "probability",
  "p_value",
  "coverage",
  "source",
] as const;

function csvCell(value: string | number | null): string {
  if (value === null) return "";
  const text = String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** Extra columns of the signal branch's site CSV, after the shared seven (same order as the server). */
export const SIGNAL_CSV_EXTRA = ["strand", "count", "ci_low", "ci_high", "max_prob", "noisyor_prob"] as const;

/**
 * Client-side CSV (used for "Download visible rows"). Ends with a newline. When any row
 * carries the signal-branch extras, the header gains the server's extra columns.
 */
export function toCsv(rows: readonly ModSite[]): string {
  const signal = anySignal(rows);
  const lines = [[...CSV_HEADER, ...(signal ? SIGNAL_CSV_EXTRA : [])].join(",")];
  for (const r of rows) {
    const cells: (string | number | null)[] = [
      r.transcript_id, r.position, r.mod_type, r.probability, r.p_value, r.coverage, r.source,
    ];
    if (signal) {
      cells.push(
        ...(isSignalSite(r)
          ? [r.strand, r.count, r.ci_low, r.ci_high, r.max_prob, r.noisyor_prob]
          : [null, null, null, null, null, null]),
      );
    }
    lines.push(cells.map(csvCell).join(","));
  }
  return lines.join("\n") + "\n";
}

/** Insert `_suffix` before the .csv extension: "a.csv" -> "a_filtered.csv". */
export function withCsvSuffix(filename: string, suffix: string): string {
  return filename.replace(/\.csv$/i, "") + `_${suffix}.csv`;
}

/** `rmodhub_sites_{transcript_id|'sequence'}_{sequence_length}nt[_suffix].csv` */
export function csvFilename(meta: Pick<PredictionMeta, "transcript_id" | "sequence_length">, suffix?: string): string {
  const id = (meta.transcript_id ?? "sequence").replace(/[^A-Za-z0-9._-]+/g, "_");
  return `rmodhub_sites_${id}_${meta.sequence_length}nt${suffix ? `_${suffix}` : ""}.csv`;
}

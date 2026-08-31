/**
 * Types mirroring the backend JSON (FastAPI, app/schemas.py and docs/signal-branch.md).
 * Captured from the real API, see ./fixtures/*.json for verbatim examples. `ModSite` is
 * the frozen cross-branch row schema shared by the sequence branch (MultiRM) and the
 * nanopore signal branch (DirectRM, source = "signal").
 */

export const MOD_TYPES = [
  "Am", "Cm", "Gm", "Um", "m1A", "m5C", "m5U", "m6A", "m6Am", "m7G", "Psi", "AtoI",
] as const;
export type ModType = (typeof MOD_TYPES)[number];

export type Source = "sequence" | "signal";

export interface ModSite {
  transcript_id: string | null;
  /** 1-based position in the sequence as entered by the user. */
  position: number;
  mod_type: ModType | string;
  /** Model probability in (0, 1]; the modification rate (count / coverage) for signal rows. */
  probability: number;
  /** Empirical p-value (multiples of 1/150 for MultiRM); null for the signal branch. */
  p_value: number | null;
  /** Reads with a score at the base (signal branch); always null for the sequence branch. */
  coverage: number | null;
  source: Source;
}

export interface AttentionWindow {
  /** 1-based inclusive. */
  start: number;
  end: number;
  score: number;
}

/** Parallels one `ModSite` row (same order as `results`). */
export interface SiteAttention {
  position: number;
  mod_type: string;
  /** Best first, non-overlapping, width 3 for MultiRM. */
  windows: AttentionWindow[];
}

export interface PredictionMeta {
  sequence_length: number;
  /** First / last position (1-based) that can receive a prediction (26 .. N-25). */
  predicted_start: number;
  predicted_end: number;
  alpha: number;
  n_sites: number;
  model_name: string;
  model_version: string;
  inference_ms: number;
  source: Source;
  transcript_id: string | null;
  mod_types: string[];
  note: string;
  extra: Record<string, unknown>;
  /** Only when the request set include_attention=true. */
  attention: SiteAttention[] | null;
}

export interface PredictResponse {
  results: ModSite[];
  meta: PredictionMeta;
}

export interface PredictRequest {
  sequence: string;
  alpha?: number;
  include_attention?: boolean;
}

export interface SampleResponse {
  name: string;
  description: string;
  sequence: string;
  length: number;
  source_url: string;
}

/**
 * Stable identity of one result row, shared by the table and the track view:
 * "position:mod_type" for sequence rows, "position:mod_type:strand" for signal rows (a
 * regions CSV may list both strands of one transcript, and the same base can then be
 * called on each strand).
 */
export function siteKey(site: Pick<ModSite, "position" | "mod_type"> & { strand?: string }): string {
  const base = `${site.position}:${site.mod_type}`;
  return typeof site.strand === "string" && site.strand !== "" ? `${base}:${site.strand}` : base;
}

/* ----------------------------------------------------------------------------------------
 * Nanopore signal branch (docs/signal-branch.md, section 6)
 * -------------------------------------------------------------------------------------- */

/** GET /api/capabilities */
export interface Capabilities {
  sequence: boolean;
  signal: boolean;
  limits: CapabilityLimits;
  retention: {
    /** Human sentence, e.g. "after feature extraction, at most 48 h". */
    inputs_deleted: string;
    results_days: number;
  };
}

export interface CapabilityLimits {
  max_pod5_gb: number;
  /** Not in the contract's key list; when absent the pod5 cap is used for the BAM too. */
  max_bam_gb?: number;
  max_reference_mb: number;
  max_regions: number;
  max_running_per_ip: number;
  max_queued_per_ip: number;
  job_timeout_h: number;
  tus_chunk_mb: number;
  /**
   * Hours an unfinished upload (job in state "uploading") is kept (RMODHUB_UPLOAD_TTL_H,
   * default 48). Reported since section 11 of docs/signal-branch.md; optional so the UI
   * keeps working against an older API (fallback 48).
   */
  upload_ttl_h?: number;
}

export type Kit = "RNA004" | "RNA002";
export const KITS: readonly Kit[] = ["RNA004", "RNA002"];

export const UPLOAD_SLOTS = ["pod5", "bam", "reference", "regions"] as const;
export type UploadSlot = (typeof UPLOAD_SLOTS)[number];

export type JobState =
  | "uploading"
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "cancelled"
  | "expired";

export type JobStage =
  | "uploading"
  | "preparing"
  | "sampling"
  | "features"
  | "denovo"
  | "inference"
  | "aggregating";

export const TERMINAL_STATES: readonly JobState[] = ["done", "failed", "cancelled", "expired"];

export function isTerminal(status: JobState): boolean {
  return TERMINAL_STATES.includes(status);
}

/** One tus upload as reported by the job (only while status == "uploading"). */
export interface UploadInfo {
  /** Same-origin path, e.g. "/api/uploads/<upload_id>". */
  url: string;
  length: number;
  offset: number;
  complete: boolean;
}

/** GET /api/jobs/{job_id} and every job-creating endpoint. */
export interface JobStatus {
  job_id: string;
  status: JobState;
  stage: JobStage | null;
  /** 0..1 within the current stage. */
  progress: number | null;
  eta_s: number | null;
  kit: Kit;
  input_kind: "upload" | "sample";
  input_bytes: Record<UploadSlot, number>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  expires_at: string | null;
  inputs_deleted_at: string | null;
  cancel_requested: boolean;
  error: string | null;
  n_sites: number | null;
  n_reads: number | null;
  n_transcripts: number | null;
  model: { name: string; version: string };
  uploads: Record<UploadSlot, UploadInfo> | null;
}

/** POST /api/jobs/signal/init body. */
export interface JobInitRequest {
  kit: Kit;
  files: Record<UploadSlot, { name: string; size: number }>;
}

/** GET /api/samples/signal */
export interface SignalSampleResponse {
  name: string;
  description: string;
  kit: Kit;
  files: { slot: UploadSlot; filename: string; bytes: number; url: string }[];
  source: "synthetic";
  regions: unknown[];
}

/** Site-level row of the signal branch: the shared ModSite fields plus DirectRM extras. */
export interface SignalSite extends ModSite {
  transcript_id: string;
  p_value: null;
  coverage: number;
  source: "signal";
  strand: string;
  /** Reads with score > 0.5. */
  count: number;
  /** 95 % Wilson score interval of probability (= rate). */
  ci_low: number;
  ci_high: number;
  max_prob: number | null;
  noisyor_prob: number | null;
}

/** Duck-typed check: does this row carry the signal-branch extras? */
export function isSignalSite(site: ModSite): site is SignalSite {
  const s = site as Partial<SignalSite>;
  return typeof s.count === "number" && typeof s.ci_low === "number" && typeof s.ci_high === "number";
}

/** Read-level row (level=read). */
export interface SignalRead {
  read_id: string;
  transcript_id: string;
  position: number;
  strand: string;
  mod_type: string;
  probability: number;
  source: "signal";
}

export interface SignalTranscript {
  transcript_id: string;
  length: number;
  n_reads: number;
  n_sites: number;
}

export interface SignalResultsMeta {
  source: "signal";
  job_id: string;
  model_name: string;
  model_version: string;
  kit: Kit;
  n_sites: number;
  n_reads: number;
  n_transcripts: number;
  mod_types: string[];
  low_coverage_threshold: number;
  transcripts: SignalTranscript[];
  /** Everything from results.sqlite `meta` (regions_skipped_low_coverage, stage_seconds, ...). */
  extra: Record<string, unknown>;
}

export interface SignalResultsPage<T> {
  results: T[];
  meta: SignalResultsMeta;
  total: number;
  offset: number;
  limit: number;
}

export type ResultsLevel = "site" | "read";

export interface ResultsQuery {
  level: ResultsLevel;
  offset?: number;
  limit?: number;
  transcript_id?: string;
  mod_type?: string;
  position?: number;
  /**
   * "+" or "-": only rows on that strand (either level). A regions CSV may list both
   * strands of one contig, so a site's read-level drill-down passes the site's strand.
   * URLSearchParams sends "+" as %2B, which is what the API requires.
   */
  strand?: string;
  min_coverage?: number;
  sort?: "position" | "rate" | "coverage" | "mod_type";
  order?: "asc" | "desc";
}

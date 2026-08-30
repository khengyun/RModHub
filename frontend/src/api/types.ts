/**
 * Types mirroring the backend JSON (FastAPI, app/schemas.py). Captured from the real API,
 * see ./fixtures/*.json for verbatim examples. `ModSite` is the frozen cross-branch row
 * schema shared with the future nanopore/DirectRM branch (source = "signal").
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
  /** Model probability in (0, 1]. */
  probability: number;
  /** Empirical p-value (multiples of 1/150 for MultiRM); may be null for the signal branch. */
  p_value: number | null;
  /** Read coverage; always null for the sequence branch. */
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

export interface HealthResponse {
  status: "ok";
  model_name: string;
  model_version: string;
  model_loaded: boolean;
  uptime_s: number;
  version: string;
}

/** Stable identity of one result row, shared by the table and the track view. */
export function siteKey(site: Pick<ModSite, "position" | "mod_type">): string {
  return `${site.position}:${site.mod_type}`;
}

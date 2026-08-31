/**
 * Pure helpers for the signal-branch pages (job status, result page): stage vocabulary,
 * time formatting, coverage warning, the synthetic per-transcript `PredictionMeta` that
 * lets the shared ResultsTable / TrackView draw one transcript at a time, and the
 * read-level CSV. No React, no DOM.
 */
import type {
  JobStage,
  JobState,
  JobStatus,
  ModSite,
  PredictionMeta,
  SignalRead,
  SignalResultsMeta,
  SignalTranscript,
} from "../../api/types";

/* ---------- polling ---------- */

export const POLL_INITIAL_MS = 2_000;
export const POLL_MAX_MS = 10_000;
export const POLL_GROWTH = 1.5;

/** 2 s, 3 s, 4.5 s, ... capped at 10 s. */
export function nextPollDelay(previousMs: number): number {
  return Math.min(POLL_MAX_MS, Math.round(previousMs * POLL_GROWTH));
}

/* ---------- results paging ---------- */

/** Server maximum per page (docs/signal-branch.md: limit <= 1000). */
export const RESULTS_PAGE_LIMIT = 1_000;
/** The page stops fetching site rows beyond this and says so (CSV has everything). */
export const MAX_SITE_ROWS = 20_000;

/* ---------- vocabulary ---------- */

export const STAGE_ORDER: readonly JobStage[] = [
  "uploading",
  "preparing",
  "sampling",
  "features",
  "denovo",
  "inference",
  "aggregating",
];

export const STAGE_INFO: Record<JobStage, { label: string; explanation: string }> = {
  uploading: {
    label: "Uploading",
    explanation: "Your four files are being transferred to the server.",
  },
  preparing: {
    label: "Preparing",
    explanation: "Indexing the BAM and checking the reference and the regions file.",
  },
  sampling: {
    label: "Sampling reads",
    explanation:
      "Selecting up to 150 reads per region; regions with 30 reads or fewer are skipped.",
  },
  features: {
    label: "Extracting features",
    explanation:
      "Aligning the raw signal of every sampled read to its bases and computing k-mer signal features (the longest stage).",
  },
  denovo: {
    label: "De novo screen",
    explanation: "Scoring every k-mer for the presence of any modification (binary DirectRM model).",
  },
  inference: {
    label: "Inference",
    explanation: "Scoring each read at each base for the six modification types.",
  },
  aggregating: {
    label: "Aggregating",
    explanation: "Combining per-read calls into per-site rates with 95 % confidence intervals.",
  },
};

export const STATUS_LABEL: Record<JobState, string> = {
  uploading: "Uploading",
  queued: "Queued",
  running: "Running",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  expired: "Expired",
};

export function stageLabel(stage: JobStage | null): string {
  return stage ? STAGE_INFO[stage].label : "—";
}

/* ---------- time ---------- */

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  const h = Math.floor(m / 60);
  return `${h} h ${String(m % 60).padStart(2, "0")} min`;
}

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  return new Date(t).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
}

/** Seconds from start (or creation) to finish (or now). */
export function elapsedSeconds(job: Pick<JobStatus, "created_at" | "started_at" | "finished_at">, nowMs: number): number {
  const start = Date.parse(job.started_at ?? job.created_at);
  const end = job.finished_at ? Date.parse(job.finished_at) : nowMs;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 0;
  return Math.max(0, (end - start) / 1000);
}

/* ---------- ids ---------- */

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isUuid(s: string): boolean {
  return UUID_RE.test(s);
}

export function shortJobId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

/* ---------- coverage ---------- */

export function lowCoverageCount(sites: readonly ModSite[], threshold: number): number {
  let n = 0;
  for (const s of sites) if (s.coverage !== null && s.coverage < threshold) n += 1;
  return n;
}

/* ---------- transcripts ---------- */

/** meta.transcripts, or derived from the rows when the server left it empty. */
export function transcriptsOf(meta: Pick<SignalResultsMeta, "transcripts">, sites: readonly ModSite[]): SignalTranscript[] {
  if (meta.transcripts && meta.transcripts.length > 0) return meta.transcripts;
  const byId = new Map<string, SignalTranscript>();
  for (const s of sites) {
    const id = s.transcript_id ?? "";
    const t = byId.get(id) ?? { transcript_id: id, length: 0, n_reads: 0, n_sites: 0 };
    t.length = Math.max(t.length, s.position);
    t.n_sites += 1;
    byId.set(id, t);
  }
  return [...byId.values()].sort((a, b) => a.transcript_id.localeCompare(b.transcript_id, "en"));
}

/**
 * The shared table/track components think in terms of one linear sequence, so each
 * transcript gets its own synthetic `PredictionMeta`: length = transcript length, the
 * whole transcript "scored" (no hatched flanks), alpha 1 (p-values do not exist here).
 */
export function transcriptMeta(
  meta: Pick<SignalResultsMeta, "model_name" | "model_version" | "mod_types" | "extra">,
  transcript: SignalTranscript,
  nSites: number,
): PredictionMeta {
  const length = Math.max(1, transcript.length);
  return {
    sequence_length: length,
    predicted_start: 1,
    predicted_end: length,
    alpha: 1,
    n_sites: nSites,
    model_name: meta.model_name,
    model_version: meta.model_version,
    inference_ms: 0,
    source: "signal",
    transcript_id: transcript.transcript_id,
    mod_types: meta.mod_types,
    note: "",
    extra: meta.extra,
    attention: null,
  };
}

/* ---------- read-level CSV ---------- */

export const READ_CSV_HEADER = [
  "read_id",
  "transcript_id",
  "position",
  "strand",
  "mod_type",
  "probability",
  "source",
] as const;

function csvCell(value: string | number | null): string {
  if (value === null) return "";
  const text = String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** Same header as the server's download.csv?level=read. Ends with a newline. */
export function readsToCsv(rows: readonly SignalRead[]): string {
  const lines = [READ_CSV_HEADER.join(",")];
  for (const r of rows) {
    lines.push(
      [r.read_id, r.transcript_id, r.position, r.strand, r.mod_type, r.probability, r.source]
        .map(csvCell)
        .join(","),
    );
  }
  return lines.join("\n") + "\n";
}

/** Per-read call: probability above 0.5 counts as modified (DirectRM read2site). */
export const READ_CALL_THRESHOLD = 0.5;

export function isCalled(read: Pick<SignalRead, "probability">): boolean {
  return read.probability > READ_CALL_THRESHOLD;
}

/* ---------- meta.extra readers ---------- */

export function extraNumber(extra: Record<string, unknown>, key: string): number | null {
  const v = extra[key];
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (Array.isArray(v)) return v.length;
  if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) return Number(v);
  return null;
}

/** Sum of `stage_seconds` when present (json object or already-parsed record). */
export function totalStageSeconds(extra: Record<string, unknown>): number | null {
  let v = extra.stage_seconds;
  if (typeof v === "string") {
    try {
      v = JSON.parse(v) as unknown;
    } catch {
      return null;
    }
  }
  if (!v || typeof v !== "object") return null;
  let sum = 0;
  let any = false;
  for (const x of Object.values(v as Record<string, unknown>)) {
    if (typeof x === "number" && Number.isFinite(x)) {
      sum += x;
      any = true;
    }
  }
  return any ? sum : null;
}

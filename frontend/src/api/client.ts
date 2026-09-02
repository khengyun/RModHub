/**
 * Thin typed client for the RModHub backend. All paths are relative: `vite dev` proxies
 * them to the API (vite.config.ts) and nginx does the same in Docker (nginx.conf).
 *
 * Sequence branch: POST /api/predict/sequence, GET /api/samples/sequence.
 * Signal branch (docs/signal-branch.md section 6): /api/capabilities, /api/samples/signal,
 * /api/jobs/... The tus upload itself lives in ./tus.ts (XMLHttpRequest for progress).
 */
import type {
  Capabilities,
  JobInitRequest,
  JobStatus,
  PredictRequest,
  PredictResponse,
  ResultsLevel,
  ResultsQuery,
  SampleResponse,
  SignalResultsPage,
  SignalSampleResponse,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  /** Human-readable message, safe to show to the user. */
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** Turn any failure (HTTP error body, network error, abort) into one readable sentence. */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof DOMException && err.name === "AbortError") return "Request cancelled.";
  if (err instanceof TypeError) {
    return "Cannot reach the RModHub server. Check your connection and try again.";
  }
  return err instanceof Error ? err.message : "Unexpected error.";
}

export const SIGNAL_DISABLED_MESSAGE = "The nanopore signal branch is not enabled on this server.";

interface ErrorOptions {
  /**
   * Signal-branch endpoints: a 503 means "branch not enabled", not "model loading", and
   * the server's one-sentence detail is the best text we have (429 quota, 409 state...).
   */
  signalBranch?: boolean;
}

async function toApiError(res: Response, opts: ErrorOptions = {}): Promise<ApiError> {
  let detail = `${res.status} ${res.statusText}`.trim();
  let hadDetail = false;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      detail = body.detail;
      hadDetail = true;
    } else if (Array.isArray(body.detail) && body.detail.length > 0) {
      const first = body.detail[0] as { msg?: string; loc?: unknown[] };
      if (first.msg) {
        detail = first.msg;
        hadDetail = true;
      }
    }
  } catch {
    /* non-JSON error body: keep the status line */
  }
  if (res.status === 503) {
    detail = opts.signalBranch
      ? hadDetail
        ? detail
        : SIGNAL_DISABLED_MESSAGE
      : "The prediction model is still loading. Please try again in a few seconds.";
  } else if (res.status === 413) {
    detail = "The request is too large for this server.";
  } else if (res.status === 404 && opts.signalBranch && !hadDetail) {
    detail = "This job is unknown or has expired.";
  } else if (res.status >= 500) {
    detail = `The server failed to process the request (${res.status}). Please try again.`;
  }
  return new ApiError(res.status, detail);
}

/* ----------------------------------------------------------------------------------------
 * Sequence branch
 * -------------------------------------------------------------------------------------- */

async function postPredict(
  req: PredictRequest,
  format: "json" | "csv",
  signal?: AbortSignal,
): Promise<Response> {
  const res = await fetch(`/api/predict/sequence?format=${format}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: format === "csv" ? "text/csv" : "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok) throw await toApiError(res);
  return res;
}

export async function predictSequence(
  req: PredictRequest,
  signal?: AbortSignal,
): Promise<PredictResponse> {
  const res = await postPredict(req, "json", signal);
  return (await res.json()) as PredictResponse;
}

/** Same rows as the JSON response, as a CSV file (backend `?format=csv`). */
export async function predictSequenceCsv(req: PredictRequest, signal?: AbortSignal): Promise<Blob> {
  const res = await postPredict(req, "csv", signal);
  return await res.blob();
}

export async function getSample(name?: string, signal?: AbortSignal): Promise<SampleResponse> {
  const url = name ? `/api/samples/sequence?name=${encodeURIComponent(name)}` : "/api/samples/sequence";
  const res = await fetch(url, { signal });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as SampleResponse;
}

/** Every example sequence, sequences included; the first entry is the server default. */
export async function getSampleCatalog(signal?: AbortSignal): Promise<SampleResponse[]> {
  const res = await fetch("/api/samples/sequence/catalog", { signal });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as SampleResponse[];
}

/* ----------------------------------------------------------------------------------------
 * Signal branch
 * -------------------------------------------------------------------------------------- */

const JSON_HEADERS = { "Content-Type": "application/json", Accept: "application/json" };

async function signalJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, { ...init, headers: { Accept: "application/json", ...(init.headers ?? {}) } });
  if (!res.ok) throw await toApiError(res, { signalBranch: true });
  return (await res.json()) as T;
}

/** GET /api/capabilities (a 404 from an older API means: signal branch absent). */
export async function getCapabilities(signal?: AbortSignal): Promise<Capabilities> {
  return signalJson<Capabilities>("/api/capabilities", { signal });
}

export async function getSignalSample(signal?: AbortSignal): Promise<SignalSampleResponse> {
  return signalJson<SignalSampleResponse>("/api/samples/signal", { signal });
}

/** POST /api/jobs/signal/sample -> 202 queued job built from the bundled synthetic sample. */
export async function createSampleJob(signal?: AbortSignal): Promise<JobStatus> {
  return signalJson<JobStatus>("/api/jobs/signal/sample", { method: "POST", signal });
}

/** POST /api/jobs/signal/init -> 201 job in state "uploading" with one tus URL per slot. */
export async function initSignalJob(req: JobInitRequest, signal?: AbortSignal): Promise<JobStatus> {
  return signalJson<JobStatus>("/api/jobs/signal/init", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(req),
    signal,
  });
}

/** POST /api/jobs/{id}/start -> 202 queued (409 while an upload is incomplete). */
export async function startJob(jobId: string, signal?: AbortSignal): Promise<JobStatus> {
  return signalJson<JobStatus>(`/api/jobs/${encodeURIComponent(jobId)}/start`, { method: "POST", signal });
}

export async function getJob(jobId: string, signal?: AbortSignal): Promise<JobStatus> {
  return signalJson<JobStatus>(`/api/jobs/${encodeURIComponent(jobId)}`, { signal, cache: "no-store" });
}

/** POST /api/jobs/{id}/cancel -> 200 cancelled (409 when already terminal). */
export async function cancelJob(jobId: string, signal?: AbortSignal): Promise<JobStatus> {
  return signalJson<JobStatus>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST", signal });
}

function resultsQuery(q: ResultsQuery): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) {
    if (v === undefined || v === null || v === "") continue;
    params.set(k, String(v));
  }
  return params.toString();
}

/**
 * GET /api/jobs/{id}/results?level=site|read&... (409 unless the job is done). Filters are
 * passed through as query parameters; `strand: "+"` is encoded as %2B by URLSearchParams
 * (a bare "+" would reach the server as a space and be refused).
 */
export async function getJobResults<T>(
  jobId: string,
  query: ResultsQuery,
  signal?: AbortSignal,
): Promise<SignalResultsPage<T>> {
  return signalJson<SignalResultsPage<T>>(
    `/api/jobs/${encodeURIComponent(jobId)}/results?${resultsQuery(query)}`,
    { signal },
  );
}

/** GET /api/jobs/{id}/download.csv?level=site|read as a Blob (streamed by the server). */
export async function getJobResultsCsv(
  jobId: string,
  level: ResultsLevel,
  signal?: AbortSignal,
): Promise<Blob> {
  const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/download.csv?level=${level}`, {
    headers: { Accept: "text/csv" },
    signal,
  });
  if (!res.ok) throw await toApiError(res, { signalBranch: true });
  return await res.blob();
}

/** Server-side filename for the CSV download (contract: rmodhub_signal_<job_id>_<level>s.csv). */
export function signalCsvFilename(jobId: string, level: ResultsLevel): string {
  return `rmodhub_signal_${jobId}_${level}s.csv`;
}

/**
 * Thin typed client for the RModHub backend. All paths are relative: `vite dev` proxies
 * them to the API (vite.config.ts) and nginx does the same in Docker (nginx.conf).
 */
import type {
  HealthResponse,
  PredictRequest,
  PredictResponse,
  SampleResponse,
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

async function toApiError(res: Response): Promise<ApiError> {
  let detail = `${res.status} ${res.statusText}`.trim();
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
    else if (Array.isArray(body.detail) && body.detail.length > 0) {
      const first = body.detail[0] as { msg?: string; loc?: unknown[] };
      detail = first.msg ?? detail;
    }
  } catch {
    /* non-JSON error body: keep the status line */
  }
  if (res.status === 503) {
    detail = "The prediction model is still loading. Please try again in a few seconds.";
  } else if (res.status === 413) {
    detail = "The request is too large. Sequences are limited to 10,000 nt.";
  } else if (res.status >= 500) {
    detail = `The server failed to process the request (${res.status}). Please try again.`;
  }
  return new ApiError(res.status, detail);
}

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

export async function getSample(signal?: AbortSignal): Promise<SampleResponse> {
  const res = await fetch("/api/samples/sequence", { signal });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as SampleResponse;
}

export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const res = await fetch("/health", { signal });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as HealthResponse;
}

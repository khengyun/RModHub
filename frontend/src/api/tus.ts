/**
 * Hand-written tus 1.0.0 client (core protocol: HEAD + PATCH, termination: DELETE), no
 * dependency.
 *
 * - XMLHttpRequest, so the browser reports upload progress per chunk.
 * - 16 MiB chunks (the server allows `tus_chunk_mb`, 64 MiB by default).
 * - Headers: Tus-Resumable 1.0.0, Upload-Offset, Content-Type application/offset+octet-stream.
 * - On any network/5xx error or an offset mismatch (409) the client asks the server for
 *   its offset (HEAD -> Upload-Offset) and continues from there. Retry delays are
 *   0 / 1 / 3 / 5 / 10 / 20 / 30 / 60 s (about two minutes in total); while the browser
 *   reports itself offline the client waits for the `online` event instead of spending
 *   retries. After the last delay the error is surfaced (the caller shows a Retry button
 *   and simply calls `tusUpload` again, which starts with a HEAD).
 * - Stall detection: a PATCH that accepts no byte for `stallMs` (60 s) is aborted and
 *   treated as a network error, so a silently dead connection (roaming, VPN reconnect)
 *   enters the same HEAD-resync retry path instead of hanging for ever. HEAD and DELETE
 *   carry a plain request timeout.
 * - Cancel with an AbortSignal: the in-flight XHR is aborted and a TusError("aborted")
 *   is thrown. Whatever the server already has stays there for a later resume.
 */

export const TUS_RESUMABLE = "1.0.0";
export const DEFAULT_CHUNK_SIZE = 16 * 1024 * 1024;
export const DEFAULT_RETRY_DELAYS_MS: readonly number[] = [0, 1000, 3000, 5000, 10_000, 20_000, 30_000, 60_000];
/** A PATCH with no accepted byte for this long is considered stalled and retried. */
export const DEFAULT_STALL_MS = 60_000;
/** Request timeout of the small control requests (HEAD, DELETE). */
export const DEFAULT_CONTROL_TIMEOUT_MS = 30_000;

export type TusErrorKind = "network" | "http" | "offset" | "aborted" | "gone";

export class TusError extends Error {
  readonly kind: TusErrorKind;
  readonly status: number | undefined;

  constructor(kind: TusErrorKind, message: string, status?: number) {
    super(message);
    this.name = "TusError";
    this.kind = kind;
    this.status = status;
  }
}

export interface TusHeadResult {
  offset: number;
  length: number | null;
}

export interface TusRequestOptions {
  signal?: AbortSignal;
  /** Test hook: build the XHR (defaults to `new XMLHttpRequest()`). */
  xhrFactory?: () => XMLHttpRequest;
  /** Request timeout of HEAD / DELETE (default 30 s; 0 disables). */
  timeoutMs?: number;
}

export interface TusRetryInfo {
  /** 1-based retry number (0 while waiting for the network to come back). */
  attempt: number;
  delayMs: number;
  error: TusError;
  /** The browser is offline: the client waits for `online` without spending a retry. */
  offline: boolean;
}

export interface TusUploadOptions extends TusRequestOptions {
  /** Same-origin upload URL from the init response, e.g. "/api/uploads/<id>". */
  url: string;
  file: Blob;
  /** Offset already known from the init/job response; otherwise a HEAD is issued first. */
  offset?: number;
  chunkSize?: number;
  retryDelaysMs?: readonly number[];
  /** Abort a PATCH that accepted no byte for this long (default 60 s; 0 disables). */
  stallMs?: number;
  onProgress?: (sent: number, total: number) => void;
  onRetry?: (info: TusRetryInfo) => void;
  /** Test hook: fake timers. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  /** Test hooks: connectivity (defaults to navigator.onLine and the window `online` event). */
  isOnline?: () => boolean;
  waitForOnline?: (signal?: AbortSignal) => Promise<void>;
}

function abortError(): TusError {
  return new TusError("aborted", "Upload cancelled.");
}

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function defaultIsOnline(): boolean {
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

/** Resolve on the window `online` event (or at once when already online / no window). */
function defaultWaitForOnline(signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    if (typeof window === "undefined" || defaultIsOnline()) {
      resolve();
      return;
    }
    const cleanup = () => {
      window.removeEventListener("online", onOnline);
      signal?.removeEventListener("abort", onAbort);
    };
    const onOnline = () => {
      cleanup();
      resolve();
    };
    const onAbort = () => {
      cleanup();
      reject(abortError());
    };
    window.addEventListener("online", onOnline);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

interface XhrResult {
  status: number;
  header: (name: string) => string | null;
}

function xhrRequest(opts: {
  method: "HEAD" | "PATCH" | "DELETE";
  url: string;
  headers: Record<string, string>;
  body?: Blob;
  signal?: AbortSignal;
  onUploadProgress?: (loaded: number) => void;
  xhrFactory?: () => XMLHttpRequest;
  /** Whole-request timeout (control requests). */
  timeoutMs?: number;
  /** Inactivity timeout between two upload-progress events (PATCH). */
  stallMs?: number;
}): Promise<XhrResult> {
  return new Promise((resolve, reject) => {
    if (opts.signal?.aborted) {
      reject(abortError());
      return;
    }
    const xhr = opts.xhrFactory ? opts.xhrFactory() : new XMLHttpRequest();
    const onAbort = () => xhr.abort();
    let stalled = false;
    let stallTimer: ReturnType<typeof setTimeout> | undefined;
    const clearStall = () => {
      if (stallTimer !== undefined) {
        clearTimeout(stallTimer);
        stallTimer = undefined;
      }
    };
    const armStall = () => {
      if (!opts.stallMs || opts.stallMs <= 0) return;
      clearStall();
      stallTimer = setTimeout(() => {
        stalled = true;
        xhr.abort();
      }, opts.stallMs);
    };
    const done = () => {
      clearStall();
      opts.signal?.removeEventListener("abort", onAbort);
    };

    xhr.open(opts.method, opts.url, true);
    for (const [k, v] of Object.entries(opts.headers)) xhr.setRequestHeader(k, v);
    if (opts.timeoutMs && opts.timeoutMs > 0) xhr.timeout = opts.timeoutMs;
    if (xhr.upload && (opts.onUploadProgress || opts.stallMs)) {
      xhr.upload.onprogress = (e: ProgressEvent) => {
        armStall();
        opts.onUploadProgress?.(e.loaded);
      };
    }
    xhr.onload = () => {
      done();
      resolve({ status: xhr.status, header: (n) => xhr.getResponseHeader(n) });
    };
    xhr.onerror = () => {
      done();
      reject(new TusError("network", "Network error during the upload."));
    };
    xhr.ontimeout = () => {
      done();
      reject(new TusError("network", "The upload request timed out."));
    };
    xhr.onabort = () => {
      done();
      if (stalled) {
        const seconds = Math.round((opts.stallMs ?? 0) / 1000);
        reject(new TusError("network", `No data was accepted for ${seconds} s; the connection seems stalled.`));
        return;
      }
      reject(abortError());
    };
    opts.signal?.addEventListener("abort", onAbort, { once: true });
    armStall();
    xhr.send(opts.body ?? null);
  });
}

function parseInt10(value: string | null): number | null {
  if (value === null) return null;
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/** HEAD: where does the server stand? */
export async function tusHead(url: string, opts: TusRequestOptions = {}): Promise<TusHeadResult> {
  const res = await xhrRequest({
    method: "HEAD",
    url,
    headers: { "Tus-Resumable": TUS_RESUMABLE },
    signal: opts.signal,
    xhrFactory: opts.xhrFactory,
    timeoutMs: opts.timeoutMs ?? DEFAULT_CONTROL_TIMEOUT_MS,
  });
  if (res.status === 200 || res.status === 204) {
    const offset = parseInt10(res.header("Upload-Offset"));
    if (offset === null) {
      throw new TusError("http", "The server did not report the upload offset.", res.status);
    }
    return { offset, length: parseInt10(res.header("Upload-Length")) };
  }
  if (res.status === 404 || res.status === 410 || res.status === 403) {
    throw new TusError(
      "gone",
      "This upload no longer exists on the server (it may have expired). Please start the job again.",
      res.status,
    );
  }
  if (res.status === 0) throw new TusError("network", "Network error during the upload.");
  throw new TusError("http", `The server could not report the upload state (HTTP ${res.status}).`, res.status);
}

/**
 * DELETE (tus termination). On this server one DELETE cancels the whole job and removes
 * every file received so far (docs/signal-branch.md section 11, item 3). Resolves when
 * the upload is gone, including when it was already gone (404/410); rejects otherwise.
 */
export async function tusDelete(url: string, opts: TusRequestOptions = {}): Promise<void> {
  const res = await xhrRequest({
    method: "DELETE",
    url,
    headers: { "Tus-Resumable": TUS_RESUMABLE },
    signal: opts.signal,
    xhrFactory: opts.xhrFactory,
    timeoutMs: opts.timeoutMs ?? DEFAULT_CONTROL_TIMEOUT_MS,
  });
  if (res.status === 204 || res.status === 200 || res.status === 404 || res.status === 410) return;
  if (res.status === 0) throw new TusError("network", "Network error while cancelling the upload.");
  throw new TusError("http", `The server could not cancel the upload (HTTP ${res.status}).`, res.status);
}

function toTusError(err: unknown): TusError {
  if (err instanceof TusError) return err;
  if (err instanceof DOMException && err.name === "AbortError") return abortError();
  return new TusError("network", err instanceof Error ? err.message : "Upload failed.");
}

function isRetryable(err: TusError): boolean {
  if (err.kind === "network" || err.kind === "offset") return true;
  if (err.kind === "http") {
    const s = err.status ?? 0;
    return s >= 500 || s === 408 || s === 423 || s === 429;
  }
  return false;
}

/**
 * Upload `file` to `url` until the server reports `Upload-Offset == file.size`.
 * Resolves when complete; rejects with a TusError (kind "aborted" on cancel).
 */
export async function tusUpload(opts: TusUploadOptions): Promise<void> {
  const {
    url,
    file,
    signal,
    xhrFactory,
    onProgress,
    onRetry,
    chunkSize = DEFAULT_CHUNK_SIZE,
    retryDelaysMs = DEFAULT_RETRY_DELAYS_MS,
    stallMs = DEFAULT_STALL_MS,
    timeoutMs = DEFAULT_CONTROL_TIMEOUT_MS,
    sleep = defaultSleep,
    isOnline = defaultIsOnline,
    waitForOnline = defaultWaitForOnline,
  } = opts;
  const total = file.size;
  let offset: number | null = typeof opts.offset === "number" && opts.offset >= 0 ? opts.offset : null;
  let attempt = 0;

  for (;;) {
    if (signal?.aborted) throw abortError();
    try {
      if (offset === null) offset = (await tusHead(url, { signal, xhrFactory, timeoutMs })).offset;
      if (offset >= total) {
        onProgress?.(total, total);
        return;
      }
      const start = offset;
      const end = Math.min(total, start + chunkSize);
      const res = await xhrRequest({
        method: "PATCH",
        url,
        headers: {
          "Tus-Resumable": TUS_RESUMABLE,
          "Upload-Offset": String(start),
          "Content-Type": "application/offset+octet-stream",
        },
        body: file.slice(start, end),
        signal,
        xhrFactory,
        stallMs,
        onUploadProgress: (loaded) => onProgress?.(Math.min(total, start + loaded), total),
      });
      if (res.status === 204 || res.status === 200) {
        const reported = parseInt10(res.header("Upload-Offset"));
        offset = reported ?? end;
        onProgress?.(Math.min(total, offset), total);
        attempt = 0;
        continue;
      }
      if (res.status === 409) {
        throw new TusError("offset", "The server reports a different upload offset; resynchronising.", 409);
      }
      if (res.status === 404 || res.status === 410 || res.status === 403) {
        throw new TusError(
          "gone",
          "This upload no longer exists on the server (it may have expired). Please start the job again.",
          res.status,
        );
      }
      if (res.status === 413) {
        throw new TusError("http", "The upload chunk is larger than this server accepts.", 413);
      }
      if (res.status === 0) throw new TusError("network", "Network error during the upload.");
      if (res.status >= 400 && res.status < 500 && ![408, 423, 429].includes(res.status)) {
        throw new TusError("http", `The server rejected the upload (HTTP ${res.status}).`, res.status);
      }
      throw new TusError("http", `The server could not store the chunk (HTTP ${res.status}).`, res.status);
    } catch (raw) {
      const err = toTusError(raw);
      if (err.kind === "aborted" || signal?.aborted) throw abortError();
      if (!isRetryable(err)) throw err;
      if (!isOnline()) {
        // Offline: a retry cannot succeed, so wait for connectivity without spending one.
        onRetry?.({ attempt, delayMs: 0, error: err, offline: true });
        await waitForOnline(signal);
        offset = null; // resynchronise with a HEAD before the next PATCH
        continue;
      }
      if (attempt >= retryDelaysMs.length) throw err;
      const delayMs = retryDelaysMs[attempt];
      attempt += 1;
      onRetry?.({ attempt, delayMs, error: err, offline: false });
      await sleep(delayMs, signal);
      offset = null; // resynchronise with a HEAD before the next PATCH
    }
  }
}

/* ----------------------------------------------------------------------------------------
 * Resume across reloads: fingerprint of a picked file
 * -------------------------------------------------------------------------------------- */

export interface FileIdentity {
  name: string;
  size: number;
  lastModified: number;
}

/** Stable key for "the same file picked again" (name + size + mtime, as tus-js-client does). */
export function fileFingerprint(f: FileIdentity): string {
  return `${f.name}|${f.size}|${f.lastModified}`;
}

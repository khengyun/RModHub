/**
 * Status card of one signal job: status pill, stage with a one-line explanation, progress
 * bar, ETA, elapsed time, timestamps, input sizes, per-slot upload offsets while uploading,
 * Cancel and Copy-link buttons. The page owns polling and the cancel request.
 */
import { useEffect, useState } from "react";
import { isTerminal, UPLOAD_SLOTS, type JobStatus } from "../../api/types";
import { formatBytes, slotDef } from "./uploadModel";
import {
  elapsedSeconds,
  formatDuration,
  formatTimestamp,
  STAGE_INFO,
  STATUS_LABEL,
} from "./signalModel";

export interface JobStatusPanelProps {
  job: JobStatus;
  /** Bookmarkable URL of this page (the only key to the job). */
  shareUrl: string;
  onCancel: () => void;
  cancelBusy: boolean;
  cancelError: string | null;
  /** Transient polling error (the last status is still shown). */
  pollError: string | null;
}

const PILL: Record<JobStatus["status"], string> = {
  uploading: "bg-sky-100 text-sky-900 border-sky-300",
  queued: "bg-slate-100 text-slate-800 border-slate-300",
  running: "bg-brand-100 text-brand-800 border-brand-600",
  done: "bg-emerald-100 text-emerald-900 border-emerald-400",
  failed: "bg-red-100 text-red-900 border-red-400",
  cancelled: "bg-amber-100 text-amber-900 border-amber-400",
  expired: "bg-slate-100 text-slate-600 border-slate-300",
};

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

export function JobStatusPanel({ job, shareUrl, onCancel, cancelBusy, cancelError, pollError }: JobStatusPanelProps) {
  const terminal = isTerminal(job.status);
  const [now, setNow] = useState(() => Date.now());
  const [copied, setCopied] = useState<"idle" | "ok" | "fail">("idle");

  // Elapsed-time ticker while the job is alive.
  useEffect(() => {
    if (terminal) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [terminal]);

  const stage = job.stage ? STAGE_INFO[job.stage] : null;
  const pct =
    job.progress === null || !Number.isFinite(job.progress)
      ? null
      : Math.max(0, Math.min(100, Math.round(job.progress * 100)));
  const elapsed = elapsedSeconds(job, terminal ? Date.parse(job.finished_at ?? job.created_at) || now : now);
  const inputTotal = UPLOAD_SLOTS.reduce((a, s) => a + (job.input_bytes?.[s] ?? 0), 0);

  const copyLink = async () => {
    setCopied((await copyText(shareUrl)) ? "ok" : "fail");
    window.setTimeout(() => setCopied("idle"), 2000);
  };

  return (
    <section
      data-testid="job-status"
      data-status={job.status}
      aria-labelledby="job-status-title"
      className="space-y-3 rounded border border-slate-200 bg-white px-4 py-3 text-sm"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h2 id="job-status-title" className="flex flex-wrap items-center gap-2 font-semibold text-brand-800">
          Job <span className="font-mono text-xs font-normal text-slate-600">{job.job_id}</span>
          <span
            data-testid="job-status-pill"
            data-status={job.status}
            role="status"
            aria-live="polite"
            className={`rounded-full border px-2 py-0.5 text-xs font-semibold uppercase tracking-wide ${PILL[job.status]}`}
          >
            {STATUS_LABEL[job.status]}
          </span>
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="copy-link"
            onClick={() => void copyLink()}
            className="rounded border border-slate-300 bg-white px-3 py-1 text-xs font-medium hover:bg-slate-50"
          >
            {copied === "ok" ? "Link copied" : copied === "fail" ? "Copy failed — use the address bar" : "Copy link"}
          </button>
          <button
            type="button"
            data-testid="job-cancel"
            onClick={onCancel}
            disabled={terminal || cancelBusy || job.cancel_requested}
            aria-busy={cancelBusy}
            className="rounded border border-red-300 bg-white px-3 py-1 text-xs font-medium text-red-800 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {job.cancel_requested && !terminal ? "Cancelling…" : cancelBusy ? "Cancelling…" : "Cancel job"}
          </button>
        </div>
      </div>

      {/* Stage + progress */}
      <div className="space-y-1.5">
        <p data-testid="job-stage" className="text-slate-800">
          <strong>Stage:</strong>{" "}
          {stage ? stage.label : job.status === "queued" ? "Waiting for a worker" : job.status === "done" ? "Finished" : "—"}
          {stage && <span className="text-slate-500"> — {stage.explanation}</span>}
          {!stage && job.status === "queued" && (
            <span className="text-slate-500"> — jobs run one at a time on a single CPU worker.</span>
          )}
        </p>
        {!terminal && (
          <div className="flex items-center gap-3">
            <div
              role="progressbar"
              data-testid="job-progress"
              aria-label="Stage progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={pct ?? undefined}
              aria-valuetext={pct === null ? "in progress" : `${pct}%`}
              className="h-2 flex-1 overflow-hidden rounded bg-slate-100"
            >
              <div
                className={`h-full bg-brand-600 ${pct === null ? "w-1/3 animate-pulse" : "transition-[width]"}`}
                style={pct === null ? undefined : { width: `${pct}%` }}
              />
            </div>
            <span className="w-12 text-right text-xs tabular-nums text-slate-600">{pct === null ? "…" : `${pct}%`}</span>
          </div>
        )}
        <p className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-slate-600">
          <span data-testid="job-elapsed">Elapsed: {formatDuration(elapsed)}</span>
          {job.eta_s !== null && !terminal && <span data-testid="job-eta">ETA: about {formatDuration(job.eta_s)}</span>}
          {job.n_sites !== null && <span>{job.n_sites.toLocaleString("en-US")} sites</span>}
        </p>
      </div>

      {job.status === "uploading" && job.uploads && (
        <ul data-testid="job-uploads" className="grid gap-1 text-xs text-slate-700 sm:grid-cols-2">
          {UPLOAD_SLOTS.map((slot) => {
            const u = job.uploads?.[slot];
            if (!u) return null;
            const p = u.length > 0 ? Math.floor((u.offset / u.length) * 100) : 0;
            return (
              <li key={slot} data-testid={`job-upload-${slot}`} className="flex items-center gap-2">
                <span className="w-20 font-medium">{slotDef(slot).short}</span>
                <span className="tabular-nums">
                  {formatBytes(u.offset)} / {formatBytes(u.length)} ({u.complete ? "complete" : `${p}%`})
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {job.status === "failed" && (
        <p data-testid="job-error" role="alert" className="rounded border border-red-300 bg-red-50 px-3 py-2 text-red-900">
          <strong>The job failed.</strong> {job.error ?? "No further detail was recorded."}
        </p>
      )}
      {job.status === "cancelled" && (
        <p data-testid="job-cancelled" className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-amber-900">
          This job was cancelled; its files have been removed.
        </p>
      )}
      {job.status === "expired" && (
        <p data-testid="job-expired" className="rounded border border-slate-300 bg-slate-50 px-3 py-2 text-slate-700">
          The results of this job have expired and were deleted.
        </p>
      )}
      {cancelError && (
        <p data-testid="job-cancel-error" role="alert" className="text-xs text-red-700">
          Could not cancel: {cancelError}
        </p>
      )}
      {pollError && !terminal && (
        <p data-testid="job-poll-error" role="status" className="text-xs text-amber-800">
          Lost contact with the server ({pollError}) — retrying automatically.
        </p>
      )}

      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-slate-600 sm:grid-cols-4">
        <div>
          <dt>Created</dt>
          <dd className="text-slate-800">{formatTimestamp(job.created_at)}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd className="text-slate-800">{formatTimestamp(job.started_at)}</dd>
        </div>
        <div>
          <dt>Finished</dt>
          <dd className="text-slate-800">{formatTimestamp(job.finished_at)}</dd>
        </div>
        <div>
          <dt>Results expire</dt>
          <dd data-testid="job-expires" className="text-slate-800">
            {formatTimestamp(job.expires_at)}
          </dd>
        </div>
        <div>
          <dt>Kit / model</dt>
          <dd className="text-slate-800">
            {job.kit} · {job.model.name} {job.model.version}
          </dd>
        </div>
        <div>
          <dt>Input</dt>
          <dd className="text-slate-800">{job.input_kind === "sample" ? "built-in sample (synthetic)" : "your upload"}</dd>
        </div>
        <div className="col-span-2">
          <dt>Input sizes</dt>
          <dd data-testid="job-inputs" className="text-slate-800">
            {UPLOAD_SLOTS.map((s) => `${slotDef(s).short} ${formatBytes(job.input_bytes?.[s] ?? 0)}`).join(" · ")} · total{" "}
            {formatBytes(inputTotal)}
            {job.inputs_deleted_at ? ` · pod5/BAM deleted ${formatTimestamp(job.inputs_deleted_at)}` : ""}
          </dd>
        </div>
      </dl>
    </section>
  );
}

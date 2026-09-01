/**
 * One of the four upload slots: file picker with accepted extensions, chosen file name and
 * size, per-file progress bar (role=progressbar), status text and a Retry button after an
 * error. Purely controlled; the page owns the state. The status text is deliberately not a
 * live region: it changes on every progress event, so the page announces slot transitions
 * through one throttled announcer instead.
 */
import { useId } from "react";
import type { UploadSlot } from "../../api/types";
import { formatBytes, percent, slotStatusText, type SlotDef, type SlotProgress } from "./uploadModel";

export interface UploadSlotFieldProps {
  def: SlotDef;
  file: File | null;
  progress: SlotProgress;
  /** Client-side validation message for this slot (missing / wrong type / too large). */
  error: string | null;
  /** "5 GB", "500 MB", "10,000 rows". */
  capLabel: string;
  disabled: boolean;
  onPick: (file: File | null) => void;
  /** Present once the slot can be retried (upload failed or was paused). */
  onRetry: (() => void) | null;
}

const BAR: Record<SlotProgress["status"], string> = {
  waiting: "bg-slate-300",
  uploading: "bg-brand-600",
  paused: "bg-amber-500",
  done: "bg-emerald-600",
  error: "bg-red-600",
};

export function UploadSlotField({ def, file, progress, error, capLabel, disabled, onPick, onRetry }: UploadSlotFieldProps) {
  const inputId = useId();
  const hintId = useId();
  const errorId = useId();
  const slot: UploadSlot = def.id;
  const pct = percent(progress);
  const showBar = progress.status !== "waiting" || file !== null;

  return (
    <div
      data-testid={`upload-slot-${slot}`}
      data-status={progress.status}
      className={`rounded border bg-white px-4 py-3 ${error ? "border-amber-400" : "border-slate-200"}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <label htmlFor={inputId} className="font-medium text-slate-800">
          {def.label}{" "}
          <span className="font-normal text-slate-500">
            ({def.extensions.join(", ")}; up to {capLabel})
          </span>
        </label>
        <span
          data-testid={`upload-status-${slot}`}
          className={`text-xs ${
            progress.status === "error"
              ? "text-red-700"
              : progress.status === "done"
                ? "text-emerald-700"
                : "text-slate-600"
          }`}
        >
          {slotStatusText(progress)}
        </span>
      </div>

      <input
        id={inputId}
        data-testid={`upload-${slot}`}
        type="file"
        accept={def.accept}
        disabled={disabled}
        aria-describedby={error ? `${hintId} ${errorId}` : hintId}
        aria-invalid={error ? true : undefined}
        onChange={(e) => onPick(e.currentTarget.files?.[0] ?? null)}
        className="mt-2 block w-full text-sm text-slate-700 file:mr-3 file:rounded file:border-0 file:bg-brand-50 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-brand-800 hover:file:bg-brand-100 disabled:opacity-50"
      />
      <p id={hintId} className="mt-1 text-xs text-slate-500">
        {def.hint}
      </p>

      {file && (
        <p data-testid={`upload-name-${slot}`} className="mt-1 text-xs text-slate-700">
          <span className="font-mono">{file.name}</span> · {formatBytes(file.size)}
        </p>
      )}

      {showBar && (
        <div className="mt-2 flex items-center gap-3">
          <div
            role="progressbar"
            data-testid={`upload-progress-${slot}`}
            aria-label={`${def.short} upload progress`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct}
            aria-valuetext={`${pct}%`}
            className="h-2 flex-1 overflow-hidden rounded bg-slate-100"
          >
            <div className={`h-full transition-[width] ${BAR[progress.status]}`} style={{ width: `${pct}%` }} />
          </div>
          <span className="w-10 text-right text-xs tabular-nums text-slate-600">{pct}%</span>
          {onRetry && (
            <button
              type="button"
              data-testid={`upload-retry-${slot}`}
              onClick={onRetry}
              className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs font-medium hover:bg-slate-50"
            >
              Retry
            </button>
          )}
        </div>
      )}

      {error && (
        <p id={errorId} data-testid={`upload-error-${slot}`} role="alert" className="mt-2 text-xs text-amber-900">
          {error}
        </p>
      )}
    </div>
  );
}

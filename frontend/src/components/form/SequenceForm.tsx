/**
 * Input form for the sequence branch: textarea, alpha, Load sample / Download sample,
 * Run / Cancel / Clear. Owned by the lead; A/B/C may propose changes but must not break
 * the data-testid hooks used by the E2E tests.
 */
import { useId } from "react";
import type { SampleResponse } from "../../api/types";
import { formatNt, MAX_NT, MIN_NT, type ClientNormalized } from "../../lib/sequence";

export interface SequenceFormProps {
  value: string;
  onChange: (value: string) => void;
  alpha: number;
  onAlphaChange: (alpha: number) => void;
  normalized: ClientNormalized;
  /** Local (pre-submit) validation message, or null when the input looks fine. */
  localError: string | null;
  busy: boolean;
  onRun: () => void;
  onCancel: () => void;
  onClear: () => void;
  /** Name of the sample to load; omitted means the server default (the first one). */
  onLoadSample: (name?: string) => void;
  onDownloadSample: () => void;
  sampleLoading: boolean;
  /**
   * Every sample this server offers. One button each when there is more than one, because
   * a 151-nt example cannot feed a model with a 601-nt window.
   */
  samples?: SampleResponse[];
}

const ALPHA_PRESETS = [0.01, 0.05, 0.1];

export function SequenceForm(p: SequenceFormProps) {
  const textId = useId();
  const alphaId = useId();
  const n = p.normalized.sequence.length;
  const canRun = !p.busy && p.localError === null && n >= MIN_NT;

  return (
    <form
      className="space-y-4"
      // Constraint validation is done in React (localError); the browser's own number-step
      // check would otherwise refuse to submit alpha values like 0.05 with step=0.01.
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        if (canRun) p.onRun();
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor={textId} className="font-medium">
          RNA / DNA sequence <span className="text-slate-500 font-normal">({MIN_NT}–{MAX_NT.toLocaleString("en-US")} nt, A/C/G/U/T, one FASTA record allowed)</span>
        </label>
        <div className="flex flex-wrap gap-2">
          {(p.samples && p.samples.length > 1 ? p.samples : [null]).map((sample, i) => (
            <button
              key={sample?.name ?? "default"}
              type="button"
              // The first button keeps the original hook: the E2E suite clicks it.
              data-testid={i === 0 ? "load-sample" : `load-sample-${sample?.name}`}
              title={sample?.description}
              onClick={() => p.onLoadSample(sample?.name)}
              disabled={p.busy || p.sampleLoading}
              className={
                i === 0
                  ? "rounded bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50"
                  : "rounded border border-brand-600 bg-white px-3 py-1.5 text-sm font-medium text-brand-700 hover:bg-brand-50 disabled:opacity-50"
              }
            >
              {p.sampleLoading && i === 0
                ? "Loading…"
                : sample
                  ? `Sample (${sample.length.toLocaleString("en-US")} nt)`
                  : "Load sample data"}
            </button>
          ))}
          <button
            type="button"
            data-testid="download-sample"
            onClick={p.onDownloadSample}
            className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50"
          >
            Download sample (FASTA)
          </button>
        </div>
      </div>

      <textarea
        id={textId}
        data-testid="sequence-input"
        value={p.value}
        onChange={(e) => p.onChange(e.target.value)}
        spellCheck={false}
        rows={6}
        placeholder={">my_transcript optional FASTA header\nGGGGCCGUGGAUACCUGCC…"}
        className="w-full rounded border border-slate-300 bg-white p-3 font-mono text-sm leading-5 focus:border-brand-600 focus:outline-none"
      />

      <div className="flex flex-wrap items-center gap-4 text-sm">
        <span data-testid="sequence-length" className="text-slate-600">
          {n > 0 ? formatNt(n) : "No sequence"}
          {p.normalized.transcriptId ? ` · id: ${p.normalized.transcriptId}` : ""}
          {p.normalized.hadU ? " · U read as T" : ""}
        </span>
        <span className="flex items-center gap-2">
          <label htmlFor={alphaId}>Significance level (alpha)</label>
          <input
            id={alphaId}
            data-testid="alpha-input"
            type="number"
            min={0.001}
            max={1}
            step="any"
            value={p.alpha}
            onChange={(e) => {
              const v = Number(e.target.value);
              if (Number.isFinite(v) && v > 0 && v <= 1) p.onAlphaChange(v);
            }}
            className="w-24 rounded border border-slate-300 px-2 py-1"
          />
          {ALPHA_PRESETS.map((a) => (
            <button
              key={a}
              type="button"
              onClick={() => p.onAlphaChange(a)}
              className={`rounded px-2 py-0.5 text-xs ${a === p.alpha ? "bg-brand-100 text-brand-800" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}
            >
              {a}
            </button>
          ))}
        </span>
      </div>

      {p.localError && (
        <p data-testid="local-error" role="alert" className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          {p.localError}
        </p>
      )}

      <div className="flex gap-2">
        <button
          type="submit"
          data-testid="run"
          disabled={!canRun}
          className="rounded bg-brand-600 px-4 py-2 font-medium text-white hover:bg-brand-700 disabled:opacity-50"
        >
          {p.busy ? "Predicting…" : "Predict modification sites"}
        </button>
        {p.busy ? (
          <button type="button" data-testid="cancel" onClick={p.onCancel} className="rounded border border-slate-300 px-4 py-2">
            Cancel
          </button>
        ) : (
          <button type="button" data-testid="clear" onClick={p.onClear} disabled={p.value === ""} className="rounded border border-slate-300 px-4 py-2 disabled:opacity-50">
            Clear
          </button>
        )}
      </div>
    </form>
  );
}

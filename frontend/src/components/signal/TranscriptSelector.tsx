import { useId } from "react";
import type { SignalTranscript } from "../../api/types";

/** Why a transcript has no site: too few reads for DirectRM (region skipped) or simply no call. */
export function emptyTranscriptReason(t: SignalTranscript, lowCoverageThreshold: number): "skipped" | "no-sites" | null {
  if (t.n_sites > 0) return null;
  return t.n_reads <= lowCoverageThreshold ? "skipped" : "no-sites";
}

/** Pick the transcript whose sites the table and the track view show (results span several). */
export function TranscriptSelector({
  transcripts,
  value,
  onChange,
  lowCoverageThreshold = 30,
}: {
  transcripts: SignalTranscript[];
  value: string;
  onChange: (transcriptId: string) => void;
  /** Regions with this many reads or fewer were skipped by DirectRM (meta.low_coverage_threshold). */
  lowCoverageThreshold?: number;
}) {
  const id = useId();
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <label htmlFor={id} className="font-medium text-slate-800">
        Transcript
      </label>
      <select
        id={id}
        data-testid="transcript-select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded border border-slate-300 bg-white px-2 py-1 text-sm focus:border-brand-600 focus:outline-none"
      >
        {transcripts.map((t) => {
          const reason = emptyTranscriptReason(t, lowCoverageThreshold);
          return (
            <option key={t.transcript_id} value={t.transcript_id}>
              {t.transcript_id} — {t.length.toLocaleString("en-US")} nt, {t.n_reads.toLocaleString("en-US")} reads,{" "}
              {t.n_sites.toLocaleString("en-US")} site{t.n_sites === 1 ? "" : "s"}
              {reason === "skipped" ? " (skipped: too few reads)" : reason === "no-sites" ? " (no site called)" : ""}
            </option>
          );
        })}
      </select>
      <span className="text-slate-500">
        {transcripts.length} transcript{transcripts.length === 1 ? "" : "s"} in this job; the CSV download
        contains all of them.
      </span>
    </div>
  );
}

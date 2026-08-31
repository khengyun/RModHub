/** Headline numbers of a finished signal job (from GET results `meta` and `meta.extra`). */
import type { SignalResultsMeta } from "../../api/types";
import { extraNumber, formatDuration, totalStageSeconds } from "./signalModel";

export function SignalSummary({ meta }: { meta: SignalResultsMeta }) {
  const skipped = extraNumber(meta.extra, "regions_skipped_low_coverage");
  const subsampled = extraNumber(meta.extra, "regions_subsampled");
  const regionsTotal = extraNumber(meta.extra, "regions_total");
  const runtime = totalStageSeconds(meta.extra);
  return (
    <dl
      data-testid="signal-summary"
      className="grid grid-cols-2 gap-x-6 gap-y-1 rounded border border-slate-200 bg-white px-4 py-3 text-sm sm:grid-cols-4"
    >
      <div>
        <dt className="text-slate-500">Sites called</dt>
        <dd data-testid="n-sites" className="text-lg font-semibold">
          {meta.n_sites.toLocaleString("en-US")}
        </dd>
      </div>
      <div>
        <dt className="text-slate-500">Transcripts / reads</dt>
        <dd>
          {meta.n_transcripts.toLocaleString("en-US")} / {meta.n_reads.toLocaleString("en-US")}
        </dd>
      </div>
      <div>
        <dt className="text-slate-500">Model</dt>
        <dd>
          {meta.model_name} <span className="text-slate-500">{meta.model_version}</span> · {meta.kit}
        </dd>
      </div>
      <div>
        <dt className="text-slate-500">Regions</dt>
        <dd data-testid="regions-summary">
          {regionsTotal !== null ? `${regionsTotal.toLocaleString("en-US")} given` : "—"}
          {skipped !== null && (
            <span className={skipped > 0 ? "text-amber-800" : "text-slate-500"}>
              {" "}
              · {skipped.toLocaleString("en-US")} skipped (≤ 30 reads)
            </span>
          )}
          {subsampled !== null && subsampled > 0 && (
            <span className="text-slate-500"> · {subsampled.toLocaleString("en-US")} subsampled to 150 reads</span>
          )}
        </dd>
      </div>
      {runtime !== null && (
        <div className="col-span-2 text-xs text-slate-500 sm:col-span-4">
          Modification types: {meta.mod_types.join(", ")} · compute time {formatDuration(runtime)}
        </div>
      )}
    </dl>
  );
}

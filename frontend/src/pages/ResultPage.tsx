/**
 * /result/:jobId — public, bookmarkable result page of a signal job. Polls the job while
 * it is alive (useJobPolling), shows stage / progress / cancel, and once the job is done
 * loads the site-level rows (paged, up to MAX_SITE_ROWS) and renders them per transcript
 * with the shared ResultsTable + TrackView, a coverage warning and the read-level panel.
 *
 * When the server reports the signal branch as disabled (capabilities.signal false, or a
 * 503 from the job route) the page says so instead of polling for ever.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, cancelJob, describeError, getJobResults, getJobResultsCsv, signalCsvFilename } from "../api/client";
import { siteKey, type ModSite, type SignalResultsMeta, type SignalSite } from "../api/types";
import { uploadTtlHours, useCapabilities } from "../components/layout/CapabilitiesProvider";
import { ResultsTable, type CsvSource } from "../components/results/ResultsTable";
import { CoverageWarning } from "../components/signal/CoverageWarning";
import { DataLifecycleNotice } from "../components/signal/DataLifecycleNotice";
import { JobStatusPanel } from "../components/signal/JobStatusPanel";
import { ReadLevelPanel } from "../components/signal/ReadLevelPanel";
import { SignalDisabled } from "../components/signal/SignalDisabled";
import { SignalSummary } from "../components/signal/SignalSummary";
import { emptyTranscriptReason, TranscriptSelector } from "../components/signal/TranscriptSelector";
import {
  extraNumber,
  isUuid,
  lowCoverageCount,
  MAX_SITE_ROWS,
  RESULTS_PAGE_LIMIT,
  transcriptMeta,
  transcriptsOf,
} from "../components/signal/signalModel";
import { useJobPolling } from "../components/signal/useJobPolling";
import { TrackView } from "../components/track/TrackView";

interface LoadedResults {
  jobId: string;
  sites: SignalSite[];
  meta: SignalResultsMeta;
  total: number;
}

const EMPTY_ATTENTION = new Map();

export function ResultPage() {
  const { jobId = "" } = useParams();
  const caps = useCapabilities();
  const { capabilities } = caps;
  const valid = isUuid(jobId);
  // The server said the branch is off: no job can exist, do not poll (the route answers 503).
  const branchOff = caps.status === "ready" && !capabilities.signal;
  const { state, refresh, replace } = useJobPolling(valid && !branchOff ? jobId : null);
  const job = state.status === "ok" ? state.job : null;

  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [results, setResults] = useState<LoadedResults | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [resultsLoading, setResultsLoading] = useState(false);

  const shareUrl = typeof window !== "undefined" ? window.location.href : `/result/${jobId}`;

  const cancel = useCallback(async () => {
    setCancelBusy(true);
    setCancelError(null);
    try {
      replace(await cancelJob(jobId));
    } catch (err) {
      setCancelError(describeError(err));
      if (err instanceof ApiError && err.status === 409) refresh();
    } finally {
      setCancelBusy(false);
    }
  }, [jobId, replace, refresh]);

  // Load every site-level row once the job is done (pages of 1000, capped).
  const done = job?.status === "done";
  useEffect(() => {
    if (!done || (results && results.jobId === jobId)) return;
    const controller = new AbortController();
    let cancelled = false;
    setResultsLoading(true);
    setResultsError(null);
    void (async () => {
      try {
        const sites: SignalSite[] = [];
        let meta: SignalResultsMeta | null = null;
        let total = 0;
        let offset = 0;
        do {
          const page = await getJobResults<SignalSite>(
            jobId,
            { level: "site", offset, limit: RESULTS_PAGE_LIMIT },
            controller.signal,
          );
          meta = page.meta;
          total = page.total;
          sites.push(...page.results);
          offset += page.results.length;
          if (page.results.length === 0) break;
        } while (offset < total && offset < MAX_SITE_ROWS);
        if (!cancelled && meta) setResults({ jobId, sites, meta, total });
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        setResultsError(describeError(err));
      } finally {
        if (!cancelled) setResultsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [done, jobId, results]);

  const uploadTtlH = uploadTtlHours(capabilities);

  if (!valid) {
    return <JobMissing jobId={jobId} days={capabilities.retention.results_days} uploadTtlH={uploadTtlH} reason="invalid" />;
  }

  if (branchOff || state.status === "unavailable") {
    return (
      <div className="space-y-4">
        <SignalDisabled detail={state.status === "unavailable" ? state.error : undefined} />
        <p data-testid="job-unavailable" className="max-w-2xl text-sm text-slate-600">
          Job <span className="font-mono">{jobId}</span> cannot be looked up while the branch is off. If it was
          created on this server, its status page will be back once an operator enables the branch again.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <section className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold text-brand-800">Nanopore signal job</h1>
        <Link to="/signal" className="text-sm text-brand-600 underline underline-offset-2">
          Start another job
        </Link>
      </section>

      {state.status === "loading" && (
        <p data-testid="job-loading" role="status" className="text-sm text-slate-600">
          Loading job status…
        </p>
      )}
      {state.status === "missing" && (
        <JobMissing jobId={jobId} days={capabilities.retention.results_days} uploadTtlH={uploadTtlH} reason="missing" />
      )}
      {state.status === "error" && (
        <div data-testid="error" role="alert" className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900">
          <strong>Could not load the job.</strong> {state.error} Retrying automatically.
        </div>
      )}

      {job && (
        <JobStatusPanel
          job={job}
          shareUrl={shareUrl}
          onCancel={() => void cancel()}
          cancelBusy={cancelBusy}
          cancelError={cancelError}
          pollError={state.status === "ok" ? state.error : null}
        />
      )}

      {done && resultsLoading && !results && (
        <p data-testid="results-loading" role="status" className="text-sm text-slate-600">
          Loading results…
        </p>
      )}
      {done && resultsError && (
        <div data-testid="results-error" role="alert" className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900">
          <strong>Could not load the results.</strong> {resultsError}{" "}
          <button type="button" className="underline" onClick={() => setResults(null)}>
            Try again
          </button>
        </div>
      )}
      {done && results && results.jobId === jobId && <SignalResults jobId={jobId} results={results} />}

      <DataLifecycleNotice capabilities={capabilities} compact />
    </div>
  );
}

function JobMissing({
  jobId,
  days,
  uploadTtlH,
  reason,
}: {
  jobId: string;
  days: number;
  uploadTtlH: number;
  reason: "missing" | "invalid";
}) {
  return (
    <div data-testid="job-missing" role="alert" className="max-w-2xl space-y-2 rounded border border-slate-200 bg-white px-4 py-4 text-sm text-slate-700">
      <h2 className="font-semibold text-brand-800">
        {reason === "invalid" ? "This is not a valid job link" : "Unknown or expired job"}
      </h2>
      <p>
        {reason === "invalid" ? (
          <>
            <span className="font-mono">{jobId || "(empty)"}</span> is not a job id. Job links look like{" "}
            <span className="font-mono">/result/8-4-4-4-12 hexadecimal characters</span>.
          </>
        ) : (
          <>
            No job <span className="font-mono">{jobId}</span> exists on this server. Results are deleted{" "}
            {days} days after a job finishes; unfinished uploads expire after {uploadTtlH} h. If you copied the
            link from somewhere, check that it is complete.
          </>
        )}
      </p>
      <p>
        <Link to="/signal" className="text-brand-600 underline underline-offset-2">
          Start a new signal job
        </Link>{" "}
        or use the{" "}
        <Link to="/" className="text-brand-600 underline underline-offset-2">
          sequence branch
        </Link>
        .
      </p>
    </div>
  );
}

function SignalResults({ jobId, results }: { jobId: string; results: LoadedResults }) {
  const { sites, meta, total } = results;
  const transcripts = useMemo(() => transcriptsOf(meta, sites), [meta, sites]);
  const firstWithSites = transcripts.find((t) => sites.some((s) => s.transcript_id === t.transcript_id));
  const [selectedTx, setSelectedTx] = useState<string>(firstWithSites?.transcript_id ?? transcripts[0]?.transcript_id ?? "");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [visible, setVisible] = useState<ModSite[]>([]);

  const txSites = useMemo(() => sites.filter((s) => s.transcript_id === selectedTx), [sites, selectedTx]);
  const transcript = transcripts.find((t) => t.transcript_id === selectedTx) ?? transcripts[0];
  const txMeta = useMemo(
    () =>
      transcriptMeta(
        meta,
        transcript ?? { transcript_id: selectedTx, length: 1, n_reads: 0, n_sites: 0 },
        txSites.length,
      ),
    [meta, transcript, selectedTx, txSites.length],
  );
  const csv = useMemo<CsvSource>(
    () => ({
      download: (signal) => getJobResultsCsv(jobId, "site", signal),
      filename: signalCsvFilename(jobId, "site"),
      totalRows: total,
    }),
    [jobId, total],
  );
  const lowCoverage = lowCoverageCount(sites, meta.low_coverage_threshold);
  const selectedSite = selectedKey ? txSites.find((s) => siteKey(s) === selectedKey) ?? null : null;
  // Regions with this many reads or fewer are dropped by DirectRM's sampling step.
  const skipThreshold = extraNumber(meta.extra, "min_coverage") ?? meta.low_coverage_threshold;
  const emptyReason = transcript ? emptyTranscriptReason(transcript, skipThreshold) : null;

  const changeTranscript = (id: string) => {
    setSelectedTx(id);
    setSelectedKey(null);
  };

  return (
    <div className="space-y-6" data-testid="results">
      <SignalSummary meta={meta} />
      {total > sites.length && (
        <p data-testid="results-truncated" className="rounded border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900">
          Showing the first {sites.length.toLocaleString("en-US")} of {total.toLocaleString("en-US")} sites in
          the browser; the CSV download contains all of them.
        </p>
      )}
      <CoverageWarning count={lowCoverage} total={sites.length} threshold={meta.low_coverage_threshold} />

      {sites.length === 0 ? (
        <div data-testid="empty" className="rounded border border-slate-200 bg-white px-4 py-6 text-sm text-slate-600">
          <strong>No site was called.</strong> Either no region had more than {skipThreshold} reads (see
          “regions skipped” above) or no base reached the model's call threshold in any read. Check that
          the regions CSV names the transcripts exactly as in the reference and the BAM.
        </div>
      ) : (
        <>
          {transcripts.length > 1 && (
            <TranscriptSelector
              transcripts={transcripts}
              value={selectedTx}
              onChange={changeTranscript}
              lowCoverageThreshold={skipThreshold}
            />
          )}
          {txSites.length === 0 ? (
            <div data-testid="transcript-empty" className="rounded border border-slate-200 bg-white px-4 py-6 text-sm text-slate-600">
              <strong>No site was called on {selectedTx}.</strong>{" "}
              {emptyReason === "skipped" ? (
                <>
                  Its region had {transcript?.n_reads.toLocaleString("en-US") ?? 0} reads in the BAM, which is{" "}
                  {skipThreshold} or fewer, so DirectRM skipped it before scoring (it needs more than {skipThreshold}{" "}
                  reads per region; see “regions skipped” above). Sequence deeper or widen the region to get calls here.
                </>
              ) : (
                <>
                  {transcript?.n_reads.toLocaleString("en-US") ?? 0} reads were available, but no base reached the
                  model's call threshold in any of them.
                </>
              )}
            </div>
          ) : (
            <>
              <TrackView
                sequence={null}
                meta={txMeta}
                sites={visible}
                attentionByKey={EMPTY_ATTENTION}
                selectedKey={selectedKey}
                onSelect={setSelectedKey}
                windowHalf={null}
              />
              <ResultsTable
                sites={txSites}
                meta={txMeta}
                csv={csv}
                selectedKey={selectedKey}
                onSelect={setSelectedKey}
                onVisibleChange={setVisible}
              />
              {selectedSite && (
                <ReadLevelPanel jobId={jobId} site={selectedSite} onClose={() => setSelectedKey(null)} />
              )}
              {!selectedSite && (
                <p className="text-xs text-slate-500">
                  Select a site in the table or the track to see the per-read calls behind it.
                </p>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}

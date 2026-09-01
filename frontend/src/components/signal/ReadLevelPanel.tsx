/**
 * Read-level drill-down for the selected site: GET results?level=read&transcript_id&position
 * &mod_type&strand, paginated server-side (ResultsPagination reused), one row per read with
 * its probability and whether it counts as modified (> 0.5). The strand is part of the
 * query because a regions CSV may list both strands of one contig, and the same base then
 * has one site per strand with different reads. CSV of the loaded page is built
 * client-side; the server's download.csv?level=read has every read of the job.
 */
import { useEffect, useRef, useState } from "react";
import { describeError, getJobResults, getJobResultsCsv, signalCsvFilename } from "../../api/client";
import type { SignalRead, SignalResultsPage, SignalSite } from "../../api/types";
import { downloadBlob, downloadText } from "../../lib/download";
import { formatProb } from "../../lib/format";
import { modTypeInfo } from "../../lib/modTypes";
import { ModTypeBadge } from "../results/ModTypeBadge";
import { ResultsPagination } from "../results/ResultsPagination";
import { isCalled, readsToCsv } from "./signalModel";

export interface ReadLevelPanelProps {
  jobId: string;
  site: SignalSite;
  onClose: () => void;
}

const DEFAULT_READ_PAGE_SIZE = 25;

export function ReadLevelPanel({ jobId, site, onClose }: ReadLevelPanelProps) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_READ_PAGE_SIZE);
  const [data, setData] = useState<SignalResultsPage<SignalRead> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [csvBusy, setCsvBusy] = useState(false);
  const [csvError, setCsvError] = useState<string | null>(null);

  const key = `${site.transcript_id}:${site.position}:${site.mod_type}:${site.strand}`;
  const sectionRef = useRef<HTMLElement>(null);

  // The panel sits below the (up to 250-row) site table: bring it into view whenever a
  // different site is selected, otherwise a row click gives no visible feedback.
  useEffect(() => {
    const el = sectionRef.current;
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }, [key]);

  // A new site starts again on page 1 (state adjusted during render).
  const [prevKey, setPrevKey] = useState(key);
  if (prevKey !== key) {
    setPrevKey(key);
    setPage(1);
  }

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const res = await getJobResults<SignalRead>(
          jobId,
          {
            level: "read",
            transcript_id: site.transcript_id,
            position: site.position,
            mod_type: site.mod_type,
            strand: site.strand,
            offset: (page - 1) * pageSize,
            limit: pageSize,
          },
          controller.signal,
        );
        if (!cancelled) setData(res);
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        setError(describeError(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [jobId, site.transcript_id, site.position, site.mod_type, site.strand, page, pageSize]);

  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const rows = data?.results ?? [];
  const info = modTypeInfo(site.mod_type);

  const downloadPage = () =>
    downloadText(
      readsToCsv(rows),
      `rmodhub_signal_${jobId}_reads_${site.transcript_id}_${site.position}_${site.mod_type}_p${page}.csv`,
      "text/csv",
    );

  const downloadAll = async () => {
    setCsvBusy(true);
    setCsvError(null);
    try {
      downloadBlob(await getJobResultsCsv(jobId, "read"), signalCsvFilename(jobId, "read"));
    } catch (err) {
      setCsvError(describeError(err));
    } finally {
      setCsvBusy(false);
    }
  };

  return (
    <section
      ref={sectionRef}
      data-testid="read-panel"
      aria-labelledby="read-panel-title"
      className="space-y-3 rounded border border-brand-100 bg-white px-4 py-3 text-sm"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 id="read-panel-title" className="flex flex-wrap items-center gap-2 font-semibold text-brand-800">
            Reads at {site.transcript_id}:{site.position.toLocaleString("en-US")} ({site.strand})
            <ModTypeBadge id={site.mod_type} />
          </h3>
          <p className="mt-0.5 text-xs text-slate-600">
            {info.label} rate {formatProb(site.probability)} (95 % CI {formatProb(site.ci_low)}–
            {formatProb(site.ci_high)}) · {site.count} of {site.coverage} reads called modified (probability
            &gt; 0.5).
          </p>
        </div>
        <button
          type="button"
          data-testid="read-panel-close"
          onClick={onClose}
          className="rounded border border-slate-300 bg-white px-2.5 py-1 text-xs hover:bg-slate-50"
        >
          Close
        </button>
      </div>

      {error && (
        <p data-testid="read-error" role="alert" className="rounded border border-red-300 bg-red-50 px-3 py-2 text-red-900">
          Could not load the reads: {error}
        </p>
      )}
      {loading && !data && (
        <p data-testid="read-loading" role="status" className="text-slate-600">
          Loading reads…
        </p>
      )}

      {data && (
        <>
          <div className="overflow-x-auto rounded border border-slate-200">
            <table data-testid="read-table" className="w-full min-w-[28rem] border-collapse text-sm" aria-busy={loading}>
              <caption className="sr-only">
                Per-read calls at {site.transcript_id} position {site.position}, {total} reads
              </caption>
              <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
                <tr>
                  <th scope="col" className="px-3 py-2 text-left font-semibold">Read id</th>
                  <th scope="col" className="px-3 py-2 text-left font-semibold">Strand</th>
                  <th scope="col" className="px-3 py-2 text-right font-semibold">Probability</th>
                  <th scope="col" className="px-3 py-2 text-left font-semibold">Called</th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="px-3 py-4 text-center text-slate-500">
                      No read-level rows for this site.
                    </td>
                  </tr>
                ) : (
                  rows.map((r) => (
                    <tr
                      key={r.read_id}
                      data-testid="read-row"
                      data-called={isCalled(r) ? "true" : "false"}
                      className="border-t border-slate-100 odd:bg-white even:bg-slate-50/60"
                    >
                      <td className="px-3 py-1 font-mono text-xs">{r.read_id}</td>
                      <td className="px-3 py-1">{r.strand}</td>
                      <td className="px-3 py-1 text-right tabular-nums">{formatProb(r.probability)}</td>
                      <td className="px-3 py-1">
                        {isCalled(r) ? (
                          <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-xs font-medium text-emerald-800">modified</span>
                        ) : (
                          <span className="text-xs text-slate-500">unmodified</span>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          <ResultsPagination
            page={page}
            pageCount={pageCount}
            pageSize={pageSize}
            start={(page - 1) * pageSize}
            shown={rows.length}
            total={total}
            onPageChange={(p) => setPage(Math.max(1, Math.min(pageCount, p)))}
            onPageSizeChange={(s) => {
              setPageSize(s);
              setPage(1);
            }}
          />
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="read-download-page"
              onClick={downloadPage}
              disabled={rows.length === 0}
              className="rounded border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
            >
              Download these {rows.length} reads (CSV)
            </button>
            <button
              type="button"
              data-testid="read-download-all"
              onClick={() => void downloadAll()}
              disabled={csvBusy}
              aria-busy={csvBusy}
              className="rounded border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
            >
              {csvBusy ? "Preparing…" : "Download all read-level calls of this job (CSV)"}
            </button>
            {csvError && (
              <span data-testid="read-download-error" role="alert" className="text-xs text-red-700">
                Download failed: {csvError}
              </span>
            )}
          </div>
        </>
      )}
    </section>
  );
}

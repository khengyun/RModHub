/**
 * Sequence branch page: form -> POST /api/predict/sequence -> summary + track view + table.
 * Owned by the lead. Handles the four UI states (idle / loading / error / success incl.
 * empty result) and the shared row selection between the table and the track view.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { describeError, getSample, predictSequence } from "../api/client";
import {
  siteKey,
  type ModSite,
  type PredictRequest,
  type PredictResponse,
  type SiteAttention,
} from "../api/types";
import { ResultsTable } from "../components/results/ResultsTable";
import { TrackView } from "../components/track/TrackView";
import { SequenceForm } from "../components/form/SequenceForm";
import { LicenseNotice } from "../components/layout/LicenseNotice";
import { downloadText } from "../lib/download";
import { formatMs } from "../lib/format";
import { MAX_NT, MIN_NT, normalizeSequenceClient, toFasta } from "../lib/sequence";

type Status = "idle" | "loading" | "success" | "error";

function localValidation(n: number, invalid: string[], multiRecord: boolean): string | null {
  if (multiRecord) {
    return "Only one sequence per request is supported: paste a single FASTA record (one '>' header line) or the bare sequence.";
  }
  if (n === 0) return null;
  if (invalid.length > 0) {
    return `Invalid character(s): ${invalid.map((c) => `'${c}'`).join(", ")}. Only A, C, G, U/T are allowed (whitespace is ignored).`;
  }
  if (n < MIN_NT) return `The sequence is ${n} nt long; at least ${MIN_NT} nt are needed (MultiRM scores 51-nt windows).`;
  if (n > MAX_NT) return `The sequence is ${n.toLocaleString("en-US")} nt long; the limit is ${MAX_NT.toLocaleString("en-US")} nt.`;
  return null;
}

export function SequencePage() {
  const [input, setInput] = useState("");
  const [alpha, setAlpha] = useState(0.05);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PredictResponse | null>(null);
  const [request, setRequest] = useState<PredictRequest | null>(null);
  const [scoredSequence, setScoredSequence] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [visible, setVisible] = useState<ModSite[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const [sampleLoading, setSampleLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const normalized = useMemo(() => normalizeSequenceClient(input), [input]);
  const localError = localValidation(
    normalized.sequence.length,
    normalized.invalidChars,
    normalized.multiRecord,
  );

  // Elapsed-time ticker while a request is in flight (long inputs take ~15 s).
  useEffect(() => {
    if (status !== "loading") return;
    const t0 = performance.now();
    const id = window.setInterval(() => setElapsed((performance.now() - t0) / 1000), 250);
    return () => window.clearInterval(id);
  }, [status]);

  const run = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const req: PredictRequest = { sequence: input, alpha, include_attention: true };
    setStatus("loading");
    setError(null);
    setSelectedKey(null);
    setElapsed(0);
    try {
      const res = await predictSequence(req, controller.signal);
      setResult(res);
      setRequest(req);
      setVisible(res.results);
      // Only hand the letters to the track view if our local normalisation agrees with the server.
      setScoredSequence(
        normalized.sequence.length === res.meta.sequence_length ? normalized.sequence : null,
      );
      setStatus("success");
    } catch (err) {
      if (controller.signal.aborted) {
        setStatus(result ? "success" : "idle");
        return;
      }
      setError(describeError(err));
      setStatus("error");
    }
  }, [input, alpha, normalized.sequence, result]);

  const cancel = useCallback(() => abortRef.current?.abort(), []);

  const clear = useCallback(() => {
    abortRef.current?.abort();
    setInput("");
    setResult(null);
    setRequest(null);
    setScoredSequence(null);
    setSelectedKey(null);
    setVisible([]);
    setError(null);
    setStatus("idle");
  }, []);

  const loadSample = useCallback(async () => {
    setSampleLoading(true);
    try {
      const sample = await getSample();
      setInput(sample.sequence);
      setError(null);
      if (status === "error") setStatus("idle");
    } catch (err) {
      setError(describeError(err));
      setStatus("error");
    } finally {
      setSampleLoading(false);
    }
  }, [status]);

  const downloadSample = useCallback(async () => {
    try {
      const sample = await getSample();
      downloadText(toFasta(sample.name, sample.sequence), `${sample.name}.fasta`, "text/x-fasta");
    } catch (err) {
      setError(describeError(err));
      setStatus("error");
    }
  }, []);

  const attentionByKey = useMemo(() => {
    const map = new Map<string, SiteAttention>();
    for (const a of result?.meta.attention ?? []) map.set(siteKey(a), a);
    return map;
  }, [result]);

  return (
    <div className="space-y-6">
      <section>
        <h1 className="text-2xl font-semibold text-brand-800">Predict RNA modification sites from a sequence</h1>
        <p className="mt-1 max-w-3xl text-sm text-slate-600">
          Paste an RNA or DNA sequence (or press <em>Load sample data</em>). MultiRM scores every
          51-nt window for 12 modification types and reports the sites whose empirical p-value is
          below your significance level.
        </p>
      </section>

      <SequenceForm
        value={input}
        onChange={setInput}
        alpha={alpha}
        onAlphaChange={setAlpha}
        normalized={normalized}
        localError={localError}
        busy={status === "loading"}
        onRun={run}
        onCancel={cancel}
        onClear={clear}
        onLoadSample={loadSample}
        onDownloadSample={downloadSample}
        sampleLoading={sampleLoading}
      />

      {status === "loading" && (
        <div data-testid="loading" role="status" className="flex items-center gap-3 rounded border border-brand-100 bg-brand-50 px-4 py-3 text-sm text-brand-800">
          <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-brand-600 border-t-transparent" aria-hidden />
          Scoring {normalized.sequence.length.toLocaleString("en-US")} nt… {elapsed.toFixed(0)} s elapsed.
          {normalized.sequence.length > 2000 && " Long sequences take up to ~15 s."}
        </div>
      )}

      {status === "error" && error && (
        <div data-testid="error" role="alert" className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900">
          <strong>Could not run the prediction.</strong> {error}
        </div>
      )}

      {status === "success" && result && request && (
        <div className="space-y-6" data-testid="results">
          <ResultsSummary result={result} />
          {result.results.length === 0 ? (
            <div data-testid="empty" className="rounded border border-slate-200 bg-white px-4 py-6 text-sm text-slate-600">
              <strong>No site passed the threshold.</strong> The sequence was scored (positions{" "}
              {result.meta.predicted_start}–{result.meta.predicted_end}), but no (position, modification)
              pair had p-value &lt; {result.meta.alpha}. Try a higher alpha (e.g. 0.1) to see weaker
              candidates — or accept that this sequence has no confident site.
            </div>
          ) : (
            <>
              <TrackView
                sequence={scoredSequence}
                meta={result.meta}
                sites={visible}
                attentionByKey={attentionByKey}
                selectedKey={selectedKey}
                onSelect={setSelectedKey}
              />
              <ResultsTable
                sites={result.results}
                meta={result.meta}
                request={request}
                selectedKey={selectedKey}
                onSelect={setSelectedKey}
                onVisibleChange={setVisible}
              />
            </>
          )}
        </div>
      )}

      {/* NAR: license + model credit must be visible on the landing page itself. */}
      <LicenseNotice />
    </div>
  );
}

function ResultsSummary({ result }: { result: PredictResponse }) {
  const m = result.meta;
  return (
    <dl data-testid="summary" className="grid grid-cols-2 gap-x-6 gap-y-1 rounded border border-slate-200 bg-white px-4 py-3 text-sm sm:grid-cols-4">
      <div><dt className="text-slate-500">Sites (p &lt; {m.alpha})</dt><dd data-testid="n-sites" className="text-lg font-semibold">{m.n_sites}</dd></div>
      <div><dt className="text-slate-500">Sequence</dt><dd>{m.sequence_length.toLocaleString("en-US")} nt{m.transcript_id ? ` · ${m.transcript_id}` : ""}</dd></div>
      <div><dt className="text-slate-500">Scored positions</dt><dd>{m.predicted_start}–{m.predicted_end} <span className="text-slate-500">(first/last 25 nt not scored)</span></dd></div>
      <div><dt className="text-slate-500">Model</dt><dd>{m.model_name} <span className="text-slate-500">{m.model_version}</span> · {formatMs(m.inference_ms)}</dd></div>
    </dl>
  );
}

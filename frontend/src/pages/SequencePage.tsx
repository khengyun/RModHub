/**
 * Sequence branch page: form -> POST /api/predict/sequence -> summary + track view + table.
 * Owned by the lead. Handles the four UI states (idle / loading / error / success incl.
 * empty result) and the shared row selection between the table and the track view.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  describeError,
  getSample,
  getSampleCatalog,
  predictSequence,
  predictSequenceCsv,
} from "../api/client";
import {
  siteKey,
  type ModelRun,
  type ModSite,
  type PredictionMeta,
  type PredictRequest,
  type PredictResponse,
  type SampleResponse,
  type SiteAttention,
} from "../api/types";
import { ResultsTable, type CsvSource } from "../components/results/ResultsTable";
import { csvFilename } from "../components/results/resultsModel";
import { TrackView } from "../components/track/TrackView";
import { SequenceForm } from "../components/form/SequenceForm";
import { ModelPicker, lengthBlocker } from "../components/form/ModelPicker";
import type { SequenceModelInfo } from "../api/types";
import {
  defaultSequenceModel,
  sequenceModels,
  useCapabilities,
} from "../components/layout/CapabilitiesProvider";
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
  const [samples, setSamples] = useState<SampleResponse[]>([]);
  const [lastSample, setLastSample] = useState<string | undefined>(undefined);
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [activeModel, setActiveModel] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const { capabilities } = useCapabilities();
  const models = sequenceModels(capabilities);
  // Start on the server's default; a deployment with one model never shows the picker.
  useEffect(() => {
    if (selectedModels.length > 0 || models.length === 0) return;
    const fallback = defaultSequenceModel(capabilities);
    if (fallback) setSelectedModels([fallback]);
  }, [models.length, capabilities, selectedModels.length]);

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
    // Only name models when the server actually offers a choice: against an older API the
    // field is left out and the default model answers, exactly as before.
    const runnable = selectedModels.filter((id) => {
      const m = models.find((x) => x.id === id);
      return m ? lengthBlocker(m, normalized.sequence.length) === null : true;
    });
    if (models.length > 1 && runnable.length > 0) req.models = runnable;
    setStatus("loading");
    setError(null);
    setSelectedKey(null);
    setElapsed(0);
    try {
      const res = await predictSequence(req, controller.signal);
      setResult(res);
      setRequest(req);
      setVisible(res.results);
      setActiveModel(res.comparison?.[0]?.model ?? null);
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
  }, [input, alpha, normalized.sequence, result, models, selectedModels]);

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

  // One button per sample when the server offers more than one; a failure here is silent
  // because the single default button keeps working through `getSample()` without a name.
  useEffect(() => {
    const controller = new AbortController();
    getSampleCatalog(controller.signal)
      .then(setSamples)
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const loadSample = useCallback(async (name?: string) => {
    setSampleLoading(true);
    setLastSample(name);
    try {
      const sample = await getSample(name);
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
      const sample = await getSample(lastSample);
      downloadText(toFasta(sample.name, sample.sequence), `${sample.name}.fasta`, "text/x-fasta");
    } catch (err) {
      setError(describeError(err));
      setStatus("error");
    }
  }, [lastSample]);

  // One uniform list whether or not the request asked for a comparison.
  const runs = useMemo<ModelRun[]>(() => {
    if (!result) return [];
    if (result.comparison && result.comparison.length > 0) return result.comparison;
    return [{ model: result.meta.model_name, results: result.results, meta: result.meta }];
  }, [result]);
  const activeRun = runs.find((r) => r.model === activeModel) ?? runs[0] ?? null;

  // Switching model re-seeds the rows the track view mirrors from the table.
  useEffect(() => {
    if (activeRun) setVisible(activeRun.results);
  }, [activeRun]);

  const attentionByKey = useMemo(() => {
    const map = new Map<string, SiteAttention>();
    for (const a of activeRun?.meta.attention ?? []) map.set(siteKey(a), a);
    return map;
  }, [activeRun]);

  // Server CSV = the same request with ?format=csv (all rows, regardless of table filters).
  const csv = useMemo<CsvSource | null>(
    () =>
      request && result
        ? {
            download: (signal) => predictSequenceCsv(request, signal),
            filename: csvFilename(result.meta),
          }
        : null,
    [request, result],
  );

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
        samples={samples}
      />

      <ModelPicker
        models={models}
        selected={selectedModels}
        onChange={setSelectedModels}
        sequenceLength={normalized.sequence.length}
        disabled={status === "loading"}
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

      {status === "success" && result && csv && activeRun && (
        <div className="space-y-6" data-testid="results">
          {runs.length > 1 && (
            <ComparisonPanel
              runs={runs}
              models={models}
              active={activeRun.model}
              onSelect={setActiveModel}
            />
          )}
          <ResultsSummary meta={activeRun.meta} />
          {activeRun.results.length === 0 ? (
            <div data-testid="empty" className="rounded border border-slate-200 bg-white px-4 py-6 text-sm text-slate-600">
              <strong>No site passed the threshold.</strong> The sequence was scored (positions{" "}
              {activeRun.meta.predicted_start}–{activeRun.meta.predicted_end}), but no (position, modification)
              pair had p-value &lt; {activeRun.meta.alpha}. Try a higher alpha (e.g. 0.1) to see weaker
              candidates — or accept that this sequence has no confident site.
            </div>
          ) : (
            <>
              <TrackView
                sequence={scoredSequence}
                meta={activeRun.meta}
                sites={visible}
                attentionByKey={attentionByKey}
                selectedKey={selectedKey}
                onSelect={setSelectedKey}
              />
              <ResultsTable
                key={activeRun.model}
                sites={activeRun.results}
                meta={activeRun.meta}
                csv={csv}
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

function ResultsSummary({ meta: m }: { meta: PredictionMeta }) {
  return (
    <dl data-testid="summary" className="grid grid-cols-2 gap-x-6 gap-y-1 rounded border border-slate-200 bg-white px-4 py-3 text-sm sm:grid-cols-4">
      <div><dt className="text-slate-500">Sites (p &lt; {m.alpha})</dt><dd data-testid="n-sites" className="text-lg font-semibold">{m.n_sites}</dd></div>
      <div><dt className="text-slate-500">Sequence</dt><dd>{m.sequence_length.toLocaleString("en-US")} nt{m.transcript_id ? ` · ${m.transcript_id}` : ""}</dd></div>
      <div><dt className="text-slate-500">Scored positions</dt><dd>{m.predicted_start}–{m.predicted_end} <span className="text-slate-500">(first/last 25 nt not scored)</span></dd></div>
      <div><dt className="text-slate-500">Model</dt><dd>{m.model_name} <span className="text-slate-500">{m.model_version}</span> · {formatMs(m.inference_ms)}</dd></div>
    </dl>
  );
}


/**
 * Head-to-head view shown when the request named several models: how many sites each one
 * reported, how much they agree, and which run the track view and table below show.
 * Agreement is counted on (position, mod_type) — the identity the results table uses.
 */
function ComparisonPanel({
  runs,
  models,
  active,
  onSelect,
}: {
  runs: ModelRun[];
  models: SequenceModelInfo[];
  active: string;
  onSelect: (id: string) => void;
}) {
  const label = (id: string) => models.find((m) => m.id === id)?.label ?? id;
  const keys = runs.map((r) => new Set(r.results.map((s) => siteKey(s))));
  const shared = [...(keys[0] ?? [])].filter((k) => keys.every((set) => set.has(k))).length;

  return (
    <section data-testid="comparison" className="rounded border border-slate-200 bg-white">
      <div className="border-b border-slate-200 px-4 py-3">
        <h2 className="text-sm font-medium text-slate-700">Model comparison</h2>
        <p className="mt-1 text-xs text-slate-500">
          Same sequence, but not the same reach: a model only scores positions its window fits
          around, so compare the counts against <em>positions scored</em> below.{" "}
          <strong>{shared}</strong> {shared === 1 ? "site was" : "sites were"} reported by every
          model; the rest are model-specific. The CSV download holds every row with a{" "}
          <code>model</code> column.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="px-4 py-2 font-medium">Model</th>
              <th scope="col" className="px-4 py-2 font-medium">Positions scored</th>
              <th scope="col" className="px-4 py-2 font-medium">Sites</th>
              <th scope="col" className="px-4 py-2 font-medium">Only this model</th>
              <th scope="col" className="px-4 py-2 font-medium">Time</th>
              <th scope="col" className="px-4 py-2 font-medium">Shown below</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run, i) => {
              const others = keys.filter((_, j) => j !== i);
              const only = [...keys[i]].filter((k) => !others.some((set) => set.has(k))).length;
              // How much of the input this model could look at at all: a 601-nt window
              // leaves 300 nt unscored at each end, a 51-nt one only 25.
              const scored = Math.max(0, run.meta.predicted_end - run.meta.predicted_start + 1);
              return (
                <tr key={run.model} className="border-t border-slate-100">
                  <th scope="row" className="px-4 py-2 text-left font-medium">
                    {label(run.model)}{" "}
                    <span className="font-normal text-slate-500">{run.meta.model_version}</span>
                  </th>
                  <td className="px-4 py-2">
                    {scored.toLocaleString("en-US")}
                    <span className="block text-xs text-slate-500">
                      {run.meta.predicted_start}\u2013{run.meta.predicted_end}
                    </span>
                  </td>
                  <td className="px-4 py-2">{run.meta.n_sites}</td>
                  <td className="px-4 py-2">{only}</td>
                  <td className="px-4 py-2">{formatMs(run.meta.inference_ms)}</td>
                  <td className="px-4 py-2">
                    <button
                      type="button"
                      data-testid={`show-${run.model}`}
                      aria-pressed={run.model === active}
                      onClick={() => onSelect(run.model)}
                      className={
                        run.model === active
                          ? "rounded bg-brand-600 px-2 py-1 text-xs font-medium text-white"
                          : "rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
                      }
                    >
                      {run.model === active ? "Showing" : "Show"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/**
 * Nanopore signal branch: upload pod5 + BAM + reference + regions, or run the bundled
 * sample, then go to /result/<job_id>.
 *
 * Flow: client-side validation -> POST /api/jobs/signal/init (names + sizes; caps and
 * quotas are enforced there) -> tus upload of the four files (two at a time, resumable,
 * see api/tus.ts) -> POST /api/jobs/<id>/start -> navigate. Interrupted uploads can be
 * resumed after a reload when the same files are picked again (localStorage fingerprint).
 *
 * A job that the page gives up on (another file picked, "discard" on the resume prompt)
 * is cancelled on the server with one tus DELETE, so abandoned attempts do not pile up
 * against the per-address quota. Leaving the page mid-upload asks for confirmation
 * (in-app navigation via the router's blocker, tab close via beforeunload).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useBlocker, useNavigate } from "react-router-dom";
import {
  ApiError,
  createSampleJob,
  describeError,
  getJob,
  getSignalSample,
  initSignalJob,
  startJob,
} from "../api/client";
import { fileFingerprint, tusDelete, tusUpload } from "../api/tus";
import {
  KITS,
  UPLOAD_SLOTS,
  type Capabilities,
  type JobStatus,
  type Kit,
  type SignalSampleResponse,
  type UploadSlot,
} from "../api/types";
import { uploadTtlHours, useCapabilities } from "../components/layout/CapabilitiesProvider";
import { ExtLink } from "../components/layout/ExtLink";
import { LicenseNotice } from "../components/layout/LicenseNotice";
import { DIRECTRM_PAPER_URL, DIRECTRM_REPO_URL } from "../components/layout/about";
import { DataLifecycleNotice } from "../components/signal/DataLifecycleNotice";
import { SignalDisabled, SignalUnavailable } from "../components/signal/SignalDisabled";
import { SubsetHelper } from "../components/signal/SubsetHelper";
import { UploadSlotField } from "../components/signal/UploadSlotField";
import {
  clearJobResumeEntries,
  findResumableJob,
  loadResumeMap,
  saveResumeEntries,
  type ResumableJob,
  type ResumeMap,
} from "../components/signal/resumeStore";
import {
  allDone,
  capLabel,
  formatBytes,
  initialSlotProgress,
  overallPercent,
  SLOT_DEFS,
  slotDef,
  uploadAnnouncement,
  validateSelection,
  type SlotProgress,
} from "../components/signal/uploadModel";
import { shortJobId } from "../components/signal/signalModel";

type Phase = "idle" | "initializing" | "uploading" | "starting" | "error";

type Files = Partial<Record<UploadSlot, File>>;
type Progress = Record<UploadSlot, SlotProgress>;
type SlotTargets = Record<UploadSlot, { url: string; offset?: number }>;

const UPLOAD_CONCURRENCY = 2;

function emptyProgress(files: Files = {}): Progress {
  return {
    pod5: initialSlotProgress(files.pod5?.size ?? 0),
    bam: initialSlotProgress(files.bam?.size ?? 0),
    reference: initialSlotProgress(files.reference?.size ?? 0),
    regions: initialSlotProgress(files.regions?.size ?? 0),
  };
}

export function SignalPage() {
  const { status, capabilities, error, refetch } = useCapabilities();
  if (status === "loading") {
    return (
      <p data-testid="signal-loading" role="status" className="text-sm text-slate-600">
        Checking which analyses this server offers…
      </p>
    );
  }
  if (status === "unavailable") return <SignalUnavailable error={error ?? null} onRetry={refetch} />;
  if (!capabilities.signal) return <SignalDisabled />;
  return <SignalUpload capabilities={capabilities} />;
}

function SignalUpload({ capabilities }: { capabilities: Capabilities }) {
  const navigate = useNavigate();
  const limits = capabilities.limits;
  const uploadTtlH = uploadTtlHours(capabilities);

  const [kit, setKit] = useState<Kit>("RNA004");
  const [files, setFiles] = useState<Files>({});
  const [progress, setProgress] = useState<Progress>(() => emptyProgress());
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [sample, setSample] = useState<SignalSampleResponse | null>(null);
  const [sampleBusy, setSampleBusy] = useState(false);
  const [resumable, setResumable] = useState<ResumableJob | null>(null);
  const [resumeDismissed, setResumeDismissed] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const targetsRef = useRef<SlotTargets | null>(null);
  /** Slots the server holds completely (kept in sync synchronously, unlike `progress`). */
  const doneSlotsRef = useRef(new Set<UploadSlot>());
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  // Sample file links (best effort; the page works without them).
  useEffect(() => {
    const controller = new AbortController();
    getSignalSample(controller.signal)
      .then((s) => setSample(Array.isArray(s?.files) ? s : null))
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const validation = useMemo(() => validateSelection(files, limits), [files, limits]);
  const busy = phase === "initializing" || phase === "uploading" || phase === "starting";
  const picked = UPLOAD_SLOTS.filter((s) => files[s]).length;
  const everythingDone = allDone(progress);
  /** A job exists whose files are still (partly) missing on the server: offer to continue it. */
  const canResume = job !== null && targetsRef.current !== null && !busy && !everythingDone;

  // "Resume previous upload" when the same four files are picked again.
  useEffect(() => {
    const map: ResumeMap = loadResumeMap(undefined, Date.now(), uploadTtlH * 3600 * 1000);
    setResumable(findResumableJob(files, map));
    setResumeDismissed(false);
  }, [files, uploadTtlH]);

  // Leaving mid-upload: confirm in-app navigation (router blocker) and tab close / reload.
  const guardRef = useRef(false);
  guardRef.current = phase === "uploading";
  const shouldBlock = useCallback(
    ({ currentLocation, nextLocation }: { currentLocation: { pathname: string }; nextLocation: { pathname: string } }) =>
      guardRef.current && currentLocation.pathname !== nextLocation.pathname,
    [],
  );
  const blocker = useBlocker(shouldBlock);
  useEffect(() => {
    // The upload finished (or was paused) while the question was open: nothing to block any more.
    if (blocker.state === "blocked" && phase !== "uploading") blocker.reset();
  }, [blocker, phase]);
  useEffect(() => {
    if (phase !== "uploading" && phase !== "starting") return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [phase]);

  // One polite live region for the whole upload, changing only on slot transitions / 10 % steps.
  const announcement = useMemo(
    () => (phase === "uploading" || phase === "starting" || phase === "error" ? uploadAnnouncement(progress) : ""),
    [phase, progress],
  );

  const setSlot = useCallback((slot: UploadSlot, patch: Partial<SlotProgress>) => {
    setProgress((prev) => ({ ...prev, [slot]: { ...prev[slot], ...patch } }));
  }, []);

  /**
   * Give up on a job the page created: stop its uploads, forget the resume record and
   * cancel it on the server (one DELETE cancels the whole job and frees the quota slot;
   * the server expires it anyway after `upload_ttl_h`, so this is best effort).
   */
  const abandonJob = useCallback((jobId: string, uploadUrl: string | null) => {
    abortRef.current?.abort();
    clearJobResumeEntries(jobId);
    if (uploadUrl) void tusDelete(uploadUrl).catch(() => undefined);
  }, []);

  const resetJob = () => {
    setJob(null);
    targetsRef.current = null;
    doneSlotsRef.current = new Set();
  };

  const pickFile = (slot: UploadSlot, file: File | null) => {
    const previous = files[slot];
    const sameFile = file !== null && previous !== undefined && fileFingerprint(file) === fileFingerprint(previous);
    const nextFiles: Files = { ...files };
    if (file) nextFiles[slot] = file;
    else delete nextFiles[slot];
    setFiles(nextFiles);
    if (phase === "error") setPhase("idle");
    setError(null);
    if (sameFile) return; // the current job (if any) still matches: keep it resumable
    if (job) {
      // The job's declared files no longer match what is picked: it can never be completed.
      abandonJob(job.job_id, targetsRef.current?.[slot]?.url ?? null);
      resetJob();
      setProgress(emptyProgress(nextFiles)); // its progress bars mean nothing any more
    } else {
      setProgress((prev) => ({ ...prev, [slot]: initialSlotProgress(file?.size ?? 0) }));
    }
  };

  /** Upload one slot; never throws. Returns true when the server has the whole file. */
  const uploadOne = useCallback(
    async (slot: UploadSlot, target: { url: string; offset?: number }, signal: AbortSignal): Promise<boolean> => {
      const file = files[slot];
      if (!file) return false;
      setSlot(slot, { status: "uploading", total: file.size, message: null });
      try {
        await tusUpload({
          url: target.url,
          file,
          offset: target.offset,
          signal,
          onProgress: (sent, total) => setSlot(slot, { sent, total, status: "uploading" }),
          onRetry: ({ attempt, delayMs, offline }) =>
            setSlot(slot, {
              message: offline ? "offline, waiting for the network" : `retry ${attempt} in ${Math.round(delayMs / 1000)} s`,
            }),
        });
        doneSlotsRef.current.add(slot);
        setSlot(slot, { status: "done", sent: file.size, total: file.size, message: null });
        return true;
      } catch (err) {
        if (signal.aborted) {
          setSlot(slot, { status: "paused", message: null });
        } else {
          setSlot(slot, { status: "error", message: err instanceof Error ? err.message : "upload failed" });
        }
        return false;
      }
    },
    [files, setSlot],
  );

  const startAndGo = useCallback(
    async (jobId: string) => {
      setPhase("starting");
      try {
        await startJob(jobId);
        clearJobResumeEntries(jobId);
        guardRef.current = false;
        navigate(`/result/${jobId}`);
      } catch (err) {
        if (!mountedRef.current) return;
        if (err instanceof ApiError && err.status === 409) {
          // The server disagrees about what it has: re-check every slot (HEAD) on resume.
          doneSlotsRef.current = new Set();
          setProgress((prev) => {
            const next = { ...prev };
            for (const s of UPLOAD_SLOTS) next[s] = { ...prev[s], status: "paused", message: null };
            return next;
          });
          setError(`${err.detail} Press Resume upload to complete them.`);
        } else {
          setError(describeError(err));
        }
        setPhase("error");
      }
    },
    [navigate],
  );

  /** Upload every slot that is not done yet (two at a time), then start the job. */
  const runUploads = useCallback(
    async (jobId: string, targets: SlotTargets, only?: UploadSlot[]) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      targetsRef.current = targets;
      setPhase("uploading");
      setError(null);

      const queue: UploadSlot[] = (only ?? [...UPLOAD_SLOTS]).filter((s) => files[s] && !doneSlotsRef.current.has(s));
      const results = new Map<UploadSlot, boolean>();
      const worker = async () => {
        for (let slot = queue.shift(); slot !== undefined; slot = queue.shift()) {
          if (controller.signal.aborted) return;
          results.set(slot, await uploadOne(slot, targets[slot], controller.signal));
        }
      };
      await Promise.all(Array.from({ length: UPLOAD_CONCURRENCY }, worker));
      if (!mountedRef.current) return;
      if (controller.signal.aborted) {
        setPhase("idle"); // paused by the user: the primary button now reads "Resume upload"
        return;
      }
      const failed = [...results.entries()].filter(([, ok]) => !ok).map(([s]) => slotDef(s).short);
      if (failed.length > 0) {
        setError(
          `${failed.join(", ")} did not finish uploading. Press Resume upload to try again, or pick the same files again later to continue.`,
        );
        setPhase("error");
        return;
      }
      if (!UPLOAD_SLOTS.every((s) => doneSlotsRef.current.has(s))) {
        setPhase("idle"); // slots outside this run are still pending: nothing failed, wait for Resume
        return;
      }
      await startAndGo(jobId);
    },
    [files, startAndGo, uploadOne],
  );

  const targetsFromJob = (j: JobStatus): SlotTargets | null => {
    if (!j.uploads) return null;
    const t = {} as SlotTargets;
    for (const slot of UPLOAD_SLOTS) {
      const u = j.uploads[slot];
      if (!u) return null;
      t[slot] = { url: u.url, offset: u.offset };
    }
    return t;
  };

  /** Continue the current job: every slot the server does not hold completely, `first` first. */
  const resumeUploads = (first?: UploadSlot) => {
    if (!job || !targetsRef.current || busy) return;
    const pending = UPLOAD_SLOTS.filter((s) => !doneSlotsRef.current.has(s));
    const order = first ? [first, ...pending.filter((s) => s !== first)] : pending;
    // Offsets may have moved since the targets were captured: every slot starts with a HEAD.
    const targets = Object.fromEntries(
      UPLOAD_SLOTS.map((s) => [s, { url: targetsRef.current![s].url }]),
    ) as SlotTargets;
    void runUploads(job.job_id, targets, order);
  };

  const submit = async () => {
    setTouched(true);
    if (busy) return;
    if (job && targetsRef.current && !everythingDone) {
      resumeUploads();
      return;
    }
    if (job && everythingDone) {
      await startAndGo(job.job_id);
      return;
    }
    if (!validation.ok) return;
    setError(null);
    setPhase("initializing");
    setProgress(emptyProgress(files));
    doneSlotsRef.current = new Set();
    try {
      const req = {
        kit,
        files: Object.fromEntries(
          UPLOAD_SLOTS.map((s) => [s, { name: files[s]!.name, size: files[s]!.size }]),
        ) as Record<UploadSlot, { name: string; size: number }>,
      };
      const created = await initSignalJob(req);
      if (!mountedRef.current) return;
      setJob(created);
      const targets = targetsFromJob(created);
      if (!targets) throw new Error("The server did not return an upload URL for every file.");
      const entries: ResumeMap = {};
      for (const slot of UPLOAD_SLOTS) {
        entries[fileFingerprint(files[slot]!)] = {
          jobId: created.job_id,
          slot,
          uploadUrl: targets[slot].url,
          savedAt: new Date().toISOString(),
        };
      }
      saveResumeEntries(entries);
      await runUploads(created.job_id, targets);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(describeError(err));
      setPhase("error");
    }
  };

  const retrySlot = (slot: UploadSlot) => resumeUploads(slot);

  const cancelUpload = () => abortRef.current?.abort();

  const resume = async () => {
    if (!resumable) return;
    setError(null);
    setPhase("initializing");
    try {
      const j = await getJob(resumable.jobId);
      if (!mountedRef.current) return;
      if (j.status === "uploading") {
        const targets = targetsFromJob(j);
        if (!targets) {
          clearJobResumeEntries(j.job_id);
          setResumable(null);
          setPhase("idle");
          setError("The server no longer lists the uploads of that job. Press Upload to start a new job.");
          return;
        }
        setJob(j);
        setProgress(emptyProgress(files));
        doneSlotsRef.current = new Set();
        await runUploads(j.job_id, targets);
      } else if (j.status === "queued" || j.status === "running" || j.status === "done") {
        clearJobResumeEntries(j.job_id);
        navigate(`/result/${j.job_id}`);
      } else {
        // failed / cancelled / expired: the upload is gone, the picked files stay in the form.
        clearJobResumeEntries(j.job_id);
        setResumable(null);
        setPhase("idle");
        setError(
          `The earlier job ${shortJobId(j.job_id)} was ${j.status} and its upload cannot be resumed. Press Upload to start a new job with these files.`,
        );
      }
    } catch (err) {
      if (!mountedRef.current) return;
      setPhase("idle");
      if (err instanceof ApiError && (err.status === 404 || err.status === 409)) {
        clearJobResumeEntries(resumable.jobId);
        setResumable(null);
        setError("The earlier upload has expired on the server. Press Upload to start a new job.");
      } else {
        // Transient (offline, proxy restarting): keep the record and the prompt.
        setError(`Could not check the earlier upload: ${describeError(err)} You can try Resume again.`);
      }
    }
  };

  const discardResumable = () => {
    if (!resumable) return;
    abandonJob(resumable.jobId, resumable.entries.pod5.uploadUrl);
    setResumable(null);
    setResumeDismissed(true);
  };

  const loadSample = async () => {
    setSampleBusy(true);
    setError(null);
    try {
      const j = await createSampleJob();
      navigate(`/result/${j.job_id}`);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(describeError(err));
      setSampleBusy(false);
    }
  };

  const showValidation = (touched || picked > 0) && validation.summary !== null;
  const primaryLabel =
    phase === "initializing"
      ? "Creating job…"
      : phase === "uploading"
        ? "Uploading…"
        : phase === "starting"
          ? "Starting…"
          : job && !everythingDone
            ? "Resume upload"
            : job && everythingDone
              ? "Start the job"
              : "Upload and start the job";

  return (
    <div data-testid="signal-page" className="space-y-6">
      <section className="max-w-3xl">
        <h1 className="text-2xl font-semibold text-brand-800">Call RNA modifications from nanopore direct-RNA signal</h1>
        <p className="mt-1 text-sm text-slate-600">
          Upload the raw signal (pod5), the basecalled and aligned reads (BAM with move table), the
          reference the reads were aligned to and the regions you care about. The server runs{" "}
          <ExtLink href={DIRECTRM_REPO_URL}>DirectRM</ExtLink> (Zhang <i>et al.</i>,{" "}
          <ExtLink href={DIRECTRM_PAPER_URL}>Nat Commun 2025</ExtLink>), which scores every read at every
          base for m6A, m5C, m1A, m7G, Ψ and ac4C and reports per-site modification rates with confidence
          intervals. Large files and a run of several minutes mean the analysis is a <em>job</em>: you
          get a result page to bookmark and can come back later.
        </p>
      </section>

      {error && (
        <div data-testid="error" role="alert" className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900">
          <strong>Could not start the job.</strong> {error}
        </div>
      )}

      {blocker.state === "blocked" && (
        <div
          data-testid="leave-prompt"
          role="alertdialog"
          aria-labelledby="leave-prompt-title"
          className="flex flex-wrap items-center gap-3 rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950"
        >
          <span id="leave-prompt-title">
            <strong>An upload is in progress.</strong> Leaving this page pauses it; the parts already received
            stay on the server for {uploadTtlH} h and you can continue by picking the same four files again.
          </span>
          <button
            type="button"
            data-testid="leave-stay"
            onClick={() => blocker.reset()}
            className="rounded bg-brand-600 px-3 py-1 text-xs font-medium text-white hover:bg-brand-700"
          >
            Stay and keep uploading
          </button>
          <button
            type="button"
            data-testid="leave-confirm"
            onClick={() => blocker.proceed()}
            className="rounded border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-50"
          >
            Leave and pause the upload
          </button>
        </div>
      )}

      {resumable && !resumeDismissed && phase === "idle" && !job && (
        <div data-testid="resume-prompt" role="status" className="flex flex-wrap items-center gap-3 rounded border border-sky-300 bg-sky-50 px-4 py-3 text-sm text-sky-950">
          <span>
            These four files belong to an upload you started earlier (job {shortJobId(resumable.jobId)}).
            Resume it where it stopped?
          </span>
          <button
            type="button"
            data-testid="resume-yes"
            onClick={() => void resume()}
            className="rounded bg-brand-600 px-3 py-1 text-xs font-medium text-white hover:bg-brand-700"
          >
            Resume previous upload
          </button>
          <button
            type="button"
            data-testid="resume-no"
            onClick={discardResumable}
            className="rounded border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-50"
          >
            Discard it and start a new job
          </button>
        </div>
      )}

      <form
        noValidate
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <fieldset className="flex flex-wrap items-center gap-4 text-sm">
            <legend className="sr-only">Sequencing kit</legend>
            <span className="font-medium">Kit</span>
            {KITS.map((k) => (
              <label key={k} className="inline-flex items-center gap-1.5">
                <input
                  type="radio"
                  name="kit"
                  data-testid={`kit-${k}`}
                  value={k}
                  checked={kit === k}
                  disabled={busy || job !== null}
                  onChange={() => setKit(k)}
                />
                {k}
                <span className="text-slate-500">{k === "RNA004" ? "(current chemistry, default)" : "(legacy)"}</span>
              </label>
            ))}
          </fieldset>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="load-sample"
              onClick={() => void loadSample()}
              disabled={busy || sampleBusy}
              className="rounded bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50"
            >
              {sampleBusy ? "Starting sample job…" : "Load sample data"}
            </button>
            {sample && sample.files.length > 0 && (
              <details data-testid="sample-files" className="relative text-sm">
                <summary className="cursor-pointer rounded border border-slate-300 bg-white px-3 py-1.5 hover:bg-slate-50">
                  Download sample files
                </summary>
                <ul className="absolute right-0 z-10 mt-1 w-72 space-y-1 rounded border border-slate-200 bg-white p-3 text-xs shadow">
                  <li className="text-slate-500">
                    {sample.name} ({sample.kit}, <strong>synthetic</strong>): {sample.description}
                  </li>
                  {sample.files.map((f) => (
                    <li key={f.slot}>
                      <a href={f.url} download={f.filename} className="text-brand-600 underline underline-offset-2">
                        {f.filename}
                      </a>{" "}
                      <span className="text-slate-500">
                        ({f.slot}, {formatBytes(f.bytes)})
                      </span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          {SLOT_DEFS.map((def) => {
            const p = progress[def.id];
            const canRetry = canResume && (p.status === "error" || p.status === "paused" || p.status === "waiting");
            return (
              <UploadSlotField
                key={def.id}
                def={def}
                file={files[def.id] ?? null}
                progress={p}
                error={touched || picked > 0 ? (validation.slotErrors[def.id] ?? null) : null}
                capLabel={capLabel(def.id, limits)}
                disabled={busy}
                onPick={(f) => pickFile(def.id, f)}
                onRetry={canRetry ? () => retrySlot(def.id) : null}
              />
            );
          })}
        </div>

        {showValidation && (
          <p data-testid="local-error" role="alert" className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            {validation.summary}
          </p>
        )}

        {/* The only live region of the upload: slot transitions and 10 % steps, not every tick. */}
        <p data-testid="upload-announcer" role="status" aria-live="polite" className="sr-only">
          {announcement}
        </p>

        {phase === "uploading" && (
          <div data-testid="upload-overall" className="flex items-center gap-3 rounded border border-brand-100 bg-brand-50 px-4 py-3 text-sm text-brand-800">
            <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-brand-600 border-t-transparent" aria-hidden />
            <span>
              Uploading… {overallPercent(progress)}% overall. Keep this tab open; if the connection drops the
              upload retries by itself for about two minutes (and waits while you are offline), and after a
              reload you can pick the same files to continue.
            </span>
          </div>
        )}
        {phase === "starting" && (
          <p role="status" className="text-sm text-slate-600">
            Upload complete — queuing the job…
          </p>
        )}
        {canResume && (
          <p data-testid="resume-hint" className="text-sm text-slate-600">
            The upload of job {shortJobId(job!.job_id)} is paused; the server keeps what it received for{" "}
            {uploadTtlH} h.
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          <button
            type="submit"
            data-testid="run"
            disabled={busy || (job === null && !validation.ok)}
            className="rounded bg-brand-600 px-4 py-2 font-medium text-white hover:bg-brand-700 disabled:opacity-50"
          >
            {primaryLabel}
          </button>
          {phase === "uploading" && (
            <button type="button" data-testid="cancel" onClick={cancelUpload} className="rounded border border-slate-300 px-4 py-2">
              Pause upload
            </button>
          )}
        </div>
      </form>

      <DataLifecycleNotice capabilities={capabilities} />
      <SubsetHelper maxPod5Gb={limits.max_pod5_gb} />
      <LicenseNotice />
    </div>
  );
}

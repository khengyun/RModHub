"""Stage runner: drives the five unmodified DirectRM scripts as subprocesses for one job.

Stage order and commands are those of ``docs/signal-branch.md`` section 2 (split name
``input``, cwd = vendor root, ``PYTHONPATH`` = vendor root, ``PYTHONHASHSEED=0``,
``--device cpu``). Each script runs in its own process group so a cancel or timeout kills it
together with anything it spawned; stdout+stderr go to ``work/logs/<stage>.log``.

Between stages the pipeline asks the database whether a cancel was requested; a heartbeat
thread refreshes ``heartbeat_at`` (plus ``progress``/``eta_s``) every ``heartbeat_interval_s``.

Every write after the claim is ``UPDATE ... WHERE status = 'running'`` (``JobDB.update_job``
with ``if_status``): the API owns every other status (``POST /cancel`` writes ``cancelled`` at
once, the reaper writes ``failed``), so a row it has closed is never touched again. A rowcount of
0 on any of these writes -- the heartbeat sees it within one interval even when the Celery
revoke was lost -- stops the job: the child process group is killed and the run unwinds as a
cancel (``JobCancelled``); the task then skips its own terminal write and removes the job dir.
"""

from __future__ import annotations

import contextlib
import csv
import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import DIRECTRM_COMMIT, MODEL_NAME, MODEL_VERSION, __version__
from .aggregate import build_results
from .config import Settings, validate_kit
from .db import RUNNING_ONLY, JobDB, NullJobDB, utcnow
from .errors import INTERRUPTS, JobCancelled, StageError
from .lifecycle import delete_inputs
from .prepare import PrepareResult, prepare_inputs

STAGES = ("preparing", "sampling", "features", "denovo", "inference", "aggregating")
SPLIT = "input"
FEATURES_PROGRESS_MARKER = "signal refinement by remora"
# Minimum spacing of progress writes to the jobs row during feature extraction; between
# these the 15 s heartbeat still refreshes progress/eta_s.
PROGRESS_WRITE_MIN_S = 2.0

NO_KMERS_MESSAGE = (
    "No usable k-mers were extracted from the sampled reads (check that the BAM has move tables "
    "and MD tags and that the pod5 matches the BAM)."
)
NO_KMERS_STD_MESSAGE = (
    "No usable k-mers were extracted from the sampled reads (a dwell-time feature has zero "
    "variance across all k-mers, which DirectRM cannot normalise; more or longer reads are needed)."
)

log = logging.getLogger("rmodhub_worker.pipeline")

_PR_SET_PDEATHSIG = 1


def _set_pdeathsig() -> None:
    """Ask Linux to SIGKILL the child if the worker process itself dies (hard time limit, OOM)."""
    with contextlib.suppress(OSError, AttributeError):
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


class StageRunner:
    """Run one command in its own process group, teeing its output to a log file."""

    def __init__(
        self,
        stage: str,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
        on_line: Callable[[str], None] | None = None,
        grace_s: float = 5.0,
    ):
        self.stage = stage
        self.cmd = [str(c) for c in cmd]
        self.cwd = Path(cwd)
        self.env = env
        self.log_path = Path(log_path)
        self.on_line = on_line
        self.grace_s = grace_s
        self.proc: subprocess.Popen | None = None
        self.returncode: int | None = None
        self.terminated = False
        self._lock = threading.Lock()

    def _pump(self, stream, log_fh) -> None:
        for raw in iter(stream.readline, b""):
            log_fh.write(raw)
            log_fh.flush()
            if self.on_line is not None:
                try:
                    self.on_line(raw.decode("utf-8", "replace"))
                except Exception:  # never let a progress parser kill the stage
                    log.exception("on_line callback failed")
        stream.close()

    def run(self) -> int:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as log_fh:
            log_fh.write(("$ " + " ".join(self.cmd) + "\n").encode())
            log_fh.flush()
            with self._lock:
                self.proc = subprocess.Popen(
                    self.cmd,
                    cwd=str(self.cwd),
                    env=self.env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=_set_pdeathsig if sys.platform.startswith("linux") else None,  # noqa: PLW1509 - one async-signal-safe prctl call
                )
            pump = threading.Thread(target=self._pump, args=(self.proc.stdout, log_fh), daemon=True)
            pump.start()
            try:
                self.returncode = self.proc.wait()
            finally:
                # Reached with the child still alive when an exception (JobCancelled from the
                # SIGTERM handler, SoftTimeLimitExceeded) interrupted ``wait()``; the lock it
                # held is released by now, so ``poll()`` sees the exit after the kill.
                if self.proc is not None and self.proc.poll() is None:
                    self.terminate()
                    self.returncode = self.proc.wait()
                pump.join(timeout=10)
        return self.returncode

    def terminate(self) -> None:
        """Kill the whole process group (SIGTERM, then SIGKILL after ``grace_s``)."""
        with self._lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self.terminated = True
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + self.grace_s
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.05)


class Heartbeat(threading.Thread):
    """Refresh ``heartbeat_at`` / ``progress`` / ``eta_s`` while the row is still ``running``.

    The UPDATE carries ``WHERE status = 'running'``; when it changes no row the API has closed
    the job (cancelled it, or the reaper declared this worker dead). The thread then calls
    ``on_lost`` once and stops -- there is nothing left to heartbeat for.
    """

    def __init__(
        self,
        db: JobDB,
        job_id: str,
        interval_s: float,
        state: Callable[[], dict[str, Any]],
        on_lost: Callable[[], None] | None = None,
    ):
        super().__init__(name="heartbeat", daemon=True)
        self.db = db
        self.job_id = job_id
        self.interval_s = interval_s
        self.state = state
        self.on_lost = on_lost
        self.lost = False
        # Not named ``_stop``: threading.Thread has a private ``_stop()`` method.
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval_s):
            try:
                changed = self.db.update_job(
                    self.job_id, if_status=RUNNING_ONLY, heartbeat_at=utcnow(), **self.state()
                )
            except Exception as exc:  # noqa: BLE001 - a DB hiccup must not stop the heartbeat
                log.warning("heartbeat update failed: %s", exc)
                continue
            if changed == 0:
                self.lost = True
                log.warning(
                    "job %s: heartbeat skipped, row no longer running (status changed by the "
                    "API); stopping the job",
                    self.job_id,
                )
                if self.on_lost is not None:
                    self.on_lost()
                return

    def stop(self) -> None:
        self._stop_event.set()


@dataclass
class PipelineResult:
    n_sites: int
    n_reads: int
    n_transcripts: int
    n_read_rows: int
    results_path: Path
    stage_seconds: dict[str, float]
    meta: dict[str, Any] = field(default_factory=dict)


class Pipeline:
    """One job, start to finish. Raises ``StageError`` / ``JobCancelled``; never leaves ``running``.

    ``JobCancelled`` (a ``BaseException``) and Celery's ``SoftTimeLimitExceeded`` arrive
    asynchronously on the main thread; every broad ``except Exception`` on that thread re-raises
    ``INTERRUPTS`` first so an interruption is never logged away or re-labelled as a failure.
    """

    def __init__(
        self,
        job_dir: Path,
        kit: str,
        *,
        settings: Settings,
        db: JobDB | None = None,
        job_id: str | None = None,
        model_id: int | None = None,
        min_coverage: int | None = None,
        max_coverage: int | None = None,
        threads: int | None = None,
        delete_inputs_after_features: bool = True,
        logger: logging.Logger | None = None,
    ):
        self.job_dir = Path(job_dir)
        self.kit = validate_kit(kit)
        self.settings = settings
        self.db: JobDB = db if db is not None else NullJobDB()
        self.job_id = job_id or self.job_dir.name
        self.model_id = int(model_id if model_id is not None else settings.directrm_model_id)
        if not 1 <= self.model_id <= 8:
            raise ValueError("model_id must be in 1..8")
        self.min_coverage = int(min_coverage if min_coverage is not None else settings.min_coverage)
        self.max_coverage = int(max_coverage if max_coverage is not None else settings.max_coverage)
        self.threads = int(threads if threads is not None else settings.worker_threads)
        self.delete_inputs_after_features = delete_inputs_after_features
        self.log = logger or log

        self.vendor = Path(settings.vendor_root)
        self.input_dir = self.job_dir / "input"
        self.work_dir = self.job_dir / "work"
        self.logs_dir = self.work_dir / "logs"

        self.stage: str | None = None
        self.progress: float | None = None
        self.eta_s: float | None = None
        self._last_progress_write = 0.0
        self.stage_seconds: dict[str, float] = {}
        self.meta: dict[str, Any] = {}
        self._runner: StageRunner | None = None
        self._cancel = threading.Event()
        self._stage_started = 0.0
        self._features_done = 0
        self._features_total = 0

    # -- control ------------------------------------------------------------------------------

    def request_cancel(self, *, kill: bool = True) -> None:
        """Cancel from another thread (kills the running child) or from a signal handler.

        A signal handler must pass ``kill=False``: it runs on the main thread *inside* the
        interrupted ``Popen.wait()``, which still holds ``_waitpid_lock``, so ``proc.poll()``
        cannot observe the child's exit and ``terminate()`` would sit through both grace
        periods. The child is killed as soon as ``wait()`` has unwound (``StageRunner.run``'s
        ``finally`` clause, then ``kill_child`` in the task's outcome handling).
        """
        self._cancel.set()
        if kill:
            self.kill_child()

    def kill_child(self) -> None:
        runner = self._runner
        if runner is not None:
            runner.terminate()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled("cancel flag set")
        try:
            requested = self.db.get_cancel_requested(self.job_id)
        except INTERRUPTS:
            raise
        except Exception as exc:  # noqa: BLE001 - DB errors never abort the job here
            self.log.warning("could not read cancel_requested_at: %s", exc)
            requested = False
        if requested:
            self._cancel.set()
            raise JobCancelled("cancel requested via database")

    def _db_state(self) -> dict[str, Any]:
        return {"progress": self.progress, "eta_s": self.eta_s}

    def _row_lost(self) -> None:
        """The jobs row is no longer ``running``: the API closed the job, so stop working on it.

        Called from whichever thread noticed it (heartbeat, the features log pump or the main
        thread); it only sets the cancel flag and kills the child, and never raises, so the
        pump thread keeps draining the child's pipe. The main thread turns the flag into
        ``JobCancelled`` as soon as the current call returns (``_run_script`` / ``_check_cancel``).
        """
        if self._cancel.is_set():
            return
        self.log.warning(
            "[%s] jobs row no longer running (status changed by the API); stopping the job",
            self.job_id,
        )
        self.request_cancel()

    def _update(self, **cols: Any) -> None:
        """Best-effort mid-run write, restricted to a row that is still ``running``."""
        try:
            changed = self.db.update_job(self.job_id, if_status=RUNNING_ONLY, **cols)
        except INTERRUPTS:
            raise
        except Exception as exc:  # noqa: BLE001 - status writes are best effort mid-run
            self.log.warning("jobs update failed (%s): %s", sorted(cols), exc)
            return
        if changed == 0:
            self._row_lost()

    def _begin_stage(self, stage: str) -> None:
        self.stage = stage
        self.progress = 0.0
        self.eta_s = None
        self._stage_started = time.monotonic()
        self._last_progress_write = self._stage_started
        self.log.info("[%s] stage %s", self.job_id, stage)
        self._update(stage=stage, progress=0.0, eta_s=None, heartbeat_at=utcnow())

    def _end_stage(self) -> None:
        assert self.stage is not None
        self.stage_seconds[self.stage] = round(time.monotonic() - self._stage_started, 3)
        self.progress = 1.0
        self.eta_s = 0.0
        self.log.info(
            "[%s] stage %s done in %.1f s", self.job_id, self.stage, self.stage_seconds[self.stage]
        )

    # -- subprocess helpers ----------------------------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": str(self.vendor),
                "PYTHONHASHSEED": "0",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "OMP_NUM_THREADS": str(self.threads),
                "MKL_NUM_THREADS": str(self.threads),
                "OPENBLAS_NUM_THREADS": str(self.threads),
                "TQDM_DISABLE": "1",
            }
        )
        return env

    def _run_script(
        self, stage: str, script: str, args: Sequence[Any], on_line=None
    ) -> StageRunner:
        cmd = [sys.executable, str(self.vendor / "scripts" / script), *[str(a) for a in args]]
        runner = StageRunner(
            stage,
            cmd,
            cwd=self.vendor,
            env=self._child_env(),
            log_path=self.logs_dir / f"{stage}.log",
            on_line=on_line,
            grace_s=self.settings.child_grace_s,
        )
        self._runner = runner
        try:
            rc = runner.run()
        finally:
            self._runner = None
        if self._cancel.is_set():
            raise JobCancelled(f"cancelled during {stage}")
        if rc != 0:
            raise StageError(
                self._failure_message(stage),
                detail=f"{script} exited with {rc}; log: {runner.log_path}",
                stage=stage,
            )
        return runner

    @staticmethod
    def _failure_message(stage: str) -> str:
        return {
            "sampling": "Read sampling failed; the BAM or the regions file could not be processed.",
            "features": "Feature extraction failed while re-squiggling the reads with Remora.",
            "denovo": "The DirectRM de novo model failed to run.",
            "inference": "The DirectRM modification-type model failed to run.",
            "aggregating": "Aggregating read-level predictions into sites failed.",
        }.get(stage, f"The job failed in stage {stage}.")

    # -- stages ----------------------------------------------------------------------------------

    def _stage_preparing(self) -> PrepareResult:
        prep = prepare_inputs(
            self.job_dir,
            max_regions=self.settings.max_regions,
            min_coverage=self.min_coverage,
            max_coverage=self.max_coverage,
            threads=self.threads,
            logger=self.log,
        )
        self.meta.update(prep.as_meta())
        return prep

    def _stage_sampling(self) -> int:
        reads_txt = self.work_dir / "reads.txt"
        self._run_script(
            "sampling",
            "sampling.py",
            [
                "--bam",
                self.input_dir,
                "--reg",
                self.input_dir / "regions.csv",
                "-o",
                reads_txt,
                "--splits",
                SPLIT,
                "--min_coverage",
                self.min_coverage,
                "--max_coverage",
                self.max_coverage,
            ],
        )
        if not reads_txt.is_file():
            raise StageError(
                self._failure_message("sampling"), detail="reads.txt missing", stage="sampling"
            )
        with reads_txt.open() as fh:
            n = len({line.strip() for line in fh if line.strip()})
        if n == 0:
            raise StageError(
                f"No region has more than {self.min_coverage} reads on the requested strand, so "
                f"there is nothing to analyse (DirectRM skips regions with {self.min_coverage} "
                "reads or fewer).",
                stage="sampling",
            )
        self.meta["n_reads_sampled"] = n
        return n

    def _on_features_line(self, line: str) -> None:
        if FEATURES_PROGRESS_MARKER in line:
            self._features_done += 1
            total = max(self._features_total, 1)
            done = min(self._features_done, total)
            self.progress = done / total
            elapsed = time.monotonic() - self._stage_started
            if done > 0:
                self.eta_s = max(0.0, elapsed / done * (total - done))
            # Flush to the jobs row at most every PROGRESS_WRITE_MIN_S so a poller sees the
            # bar move between heartbeats (a stage shorter than the heartbeat would
            # otherwise never report anything but 0.0).
            now = time.monotonic()
            if now - self._last_progress_write >= PROGRESS_WRITE_MIN_S:
                self._last_progress_write = now
                self._update(progress=self.progress, eta_s=self.eta_s, heartbeat_at=utcnow())

    def _stage_features(self, n_sampled: int) -> None:
        self._features_total = n_sampled
        self._features_done = 0
        features_dir = self.work_dir / "features"
        self._run_script(
            "features",
            "feature_extraction.py",
            [
                "--pod5_dir",
                self.input_dir,
                "--bam",
                self.input_dir,
                "--reg",
                self.input_dir / "regions.csv",
                "--level",
                self.settings.level_table(self.kit),
                "-o",
                features_dir,
                "--splits",
                SPLIT,
                "--read_ids",
                self.work_dir / "reads.txt",
                "--kmer",
                9,
                "--step",
                5,
            ],
            on_line=self._on_features_line,
        )
        n_kmers, n_reads_features = check_features(features_dir)
        self.meta["n_kmers"] = n_kmers
        self.meta["n_reads_features"] = n_reads_features
        self.meta["n_reads_resquiggled"] = self._features_done

    def _stage_denovo(self) -> None:
        import numpy as np

        denovo_dir = self.work_dir / "denovo"
        self._run_script(
            "denovo",
            "denovo_inference.py",
            [
                "--feature_dir",
                self.work_dir / "features",
                "--outdir",
                denovo_dir,
                "--model_path",
                self.settings.denovo_model(self.kit),
                "--splits",
                SPLIT,
                "--device",
                "cpu",
            ],
        )
        npy = denovo_dir / f"{SPLIT}_denovo.npy"
        frac: float | None = None
        if npy.is_file():
            probs = np.load(npy)
            if probs.size:
                frac = float(np.mean(probs >= 0.5))
        else:
            self.log.warning("[%s] %s not written by denovo_inference.py", self.job_id, npy.name)
        self.meta["denovo_frac_modified"] = frac

    def _stage_inference(self) -> None:
        self._run_script(
            "inference",
            "inference.py",
            [
                "--feature_dir",
                self.work_dir / "features",
                "--outdir",
                self.work_dir / "inference",
                "--device",
                "cpu",
                "--splits",
                SPLIT,
                "--ml",
                "True",
                "--model_dir",
                self.settings.model_dir(self.kit),
                "--model_id",
                self.model_id,
            ],
        )

    def _stage_aggregating(self, prep: PrepareResult) -> PipelineResult:
        import numpy
        import pysam
        import remora
        import torch

        sites_dir = self.work_dir / "sites"
        inference_dir = self.work_dir / "inference"
        self._run_script(
            "aggregating",
            "read2site.py",
            ["--indir", inference_dir, "--outdir", sites_dir, "--delete", "False"],
        )
        meta: dict[str, Any] = {
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "kit": self.kit,
            "directrm_commit": DIRECTRM_COMMIT,
            "directrm_model_id": self.model_id,
            "remora_version": getattr(remora, "__version__", "unknown"),
            "torch_version": torch.__version__,
            "numpy_version": numpy.__version__,
            "pysam_version": pysam.__version__,
            "python_version": sys.version.split()[0],
            "worker_version": __version__,
            "min_coverage": self.min_coverage,
            "max_coverage": self.max_coverage,
            "threads": self.threads,
        }
        meta.update(self.meta)
        # ``aggregating`` covers read2site + the time spent so far (the sqlite write itself
        # cannot be included in a file it is part of); run() keeps the exact figure.
        stage_seconds = dict(self.stage_seconds)
        stage_seconds["aggregating"] = round(time.monotonic() - self._stage_started, 3)
        meta["stage_seconds"] = stage_seconds
        n_sites, n_read_rows, n_transcripts = build_results(
            self.job_dir,
            meta=meta,
            transcripts=prep.transcripts(),
            sites_dir=sites_dir,
            inference_dir=inference_dir,
            regions=prep.region_rows(),
        )
        return PipelineResult(
            n_sites=n_sites,
            n_reads=int(self.meta.get("n_reads_features", 0)),
            n_transcripts=n_transcripts,
            n_read_rows=n_read_rows,
            results_path=self.job_dir / "results.sqlite",
            stage_seconds=self.stage_seconds,
            meta=meta,
        )

    # -- driver ----------------------------------------------------------------------------------

    def run(self) -> PipelineResult:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        heartbeat = Heartbeat(
            self.db,
            self.job_id,
            self.settings.heartbeat_interval_s,
            self._db_state,
            on_lost=self._row_lost,
        )
        heartbeat.start()
        try:
            self._check_cancel()
            self._begin_stage("preparing")
            prep = self._stage_preparing()
            self._end_stage()

            self._check_cancel()
            self._begin_stage("sampling")
            n_sampled = self._stage_sampling()
            self._end_stage()

            self._check_cancel()
            self._begin_stage("features")
            self._stage_features(n_sampled)
            self._end_stage()
            if self.delete_inputs_after_features:
                removed = delete_inputs(self.job_dir)
                self.log.info("[%s] deleted inputs after features: %s", self.job_id, removed)
                self._update(inputs_deleted_at=utcnow())

            self._check_cancel()
            self._begin_stage("denovo")
            self._stage_denovo()
            self._end_stage()

            self._check_cancel()
            self._begin_stage("inference")
            self._stage_inference()
            self._end_stage()

            self._check_cancel()
            self._begin_stage("aggregating")
            result = self._stage_aggregating(prep)
            self._end_stage()
            result.stage_seconds = dict(self.stage_seconds)
            return result
        except StageError as exc:
            if exc.stage is None:
                exc.stage = self.stage
            raise
        finally:
            heartbeat.stop()
            heartbeat.join(timeout=5)


def check_features(features_dir: Path) -> tuple[int, int]:
    """Guard against the two upstream crash modes after ``feature_extraction.py``.

    Returns ``(n_kmers, n_reads_with_features)``. Raises ``StageError`` if the split has no
    k-mers (0-length arrays / ``\\n``-only CSV, which crash ``denovo_inference.py`` and
    ``inference.py``) or if a dwell column has zero variance (the per-split z-score would be NaN).
    """
    import numpy as np

    npz = Path(features_dir) / f"{SPLIT}.npz"
    csv_path = Path(features_dir) / f"{SPLIT}.csv"
    if not npz.is_file() or not csv_path.is_file():
        raise StageError(
            NO_KMERS_MESSAGE, detail="features/input.{npz,csv} missing", stage="features"
        )
    with np.load(npz) as data:
        if "stat" not in data.files:
            raise StageError(NO_KMERS_MESSAGE, detail="npz without 'stat'", stage="features")
        stat = data["stat"]
    if stat.ndim != 3 or stat.shape[0] == 0:
        raise StageError(NO_KMERS_MESSAGE, detail=f"stat shape {stat.shape}", stage="features")
    dwell = stat[:, :, 2].astype("float64")
    if np.isnan(dwell).any():
        raise StageError(NO_KMERS_MESSAGE, detail="NaN dwell values", stage="features")
    stds = dwell.std(axis=0)
    if bool((stds == 0).any()):
        raise StageError(
            NO_KMERS_STD_MESSAGE, detail=f"dwell std per position {stds.tolist()}", stage="features"
        )
    n_kmers = int(stat.shape[0])
    read_ids = set()
    with csv_path.open(newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header is literally 0,1,2,3,4,5
        for row in reader:
            if row:
                read_ids.add(row[0])
    return n_kmers, len(read_ids)

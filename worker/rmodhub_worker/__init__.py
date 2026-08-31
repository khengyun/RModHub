"""RModHub nanopore signal worker.

Runs the vendored DirectRM pipeline (``directrm_vendor/``) as subprocesses for one job at a
time and publishes ``results.sqlite`` per ``docs/signal-branch.md``. The package never imports
the API (``app/``); it talks to Postgres with plain SQL (``db.py``) and receives work by Celery
task name (``tasks.py``) or from the command line (``run_local.py``).
"""

__version__ = "0.1.0"

MODEL_NAME = "DirectRM"
MODEL_VERSION = "bc7a085"
DIRECTRM_COMMIT = "bc7a08573dfe7629e808256fa6ade6e4111ed1f9"
TASK_NAME = "rmodhub.signal.run_job"

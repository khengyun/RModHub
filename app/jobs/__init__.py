"""Nanopore signal branch: asynchronous DirectRM jobs.

The API side only (Python 3.12): job metadata in Postgres/SQLite (`models`), resumable and
multipart uploads on the shared volume (`storage`), per-address quotas (`quota`), the Celery
producer (`queue`), the job state machine (`service`), read-only access to each job's
`results.sqlite` (`results`) and the data-lifecycle reaper (`cleanup`). The worker never
imports this package; the contract between the two is docs/signal-branch.md.
"""

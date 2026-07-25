"""
importer/job_runner.py
Generic in-memory background-job registry, used by picking_api.py to run
long-running work (currently: market price refresh) on a daemon thread
instead of blocking an HTTP request for minutes. The frontend Jobs page
polls GET /api/jobs / /api/jobs/{id} instead of waiting on the POST that
starts a job.

State is in-memory only — a picking_api.py restart loses in-flight and
historical job state, same tradeoff already accepted elsewhere in this
service (see docs/plans/listing-pricing-system.md). Only the most recent
JOB_HISTORY_LIMIT jobs are kept; older ones are pruned as new ones start.

Adding a new job type: write a function that accepts job_id as a keyword
arg and calls update_job(job_id, ...) as it makes progress, then wire one
POST endpoint in picking_api.py that calls start_job(job_type, label,
that_function, **params). It automatically shows up in the generic
GET /api/jobs list the Jobs page already polls — no frontend changes
needed for a new job type's progress display, only for its "start" form.
"""

import threading
import time
import uuid

JOB_HISTORY_LIMIT = 50

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def start_job(job_type: str, label: str, target, **kwargs) -> str:
    """
    Registers a new job and runs target(job_id=..., **kwargs) on a daemon
    thread. target must return a dict on success (becomes the job's
    `result`); a raised exception marks the job failed with str(e) as
    `error`.
    """
    job_id = str(uuid.uuid4())
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "job_type": job_type,
            "label": label,
            "status": "running",
            "progress": {},
            "result": None,
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        }
        _prune_locked()

    def _run():
        try:
            result = target(job_id=job_id, **kwargs)
            with _lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["result"] = result
                _jobs[job_id]["finished_at"] = time.time()
        except Exception as e:
            with _lock:
                _jobs[job_id]["status"] = "failed"
                _jobs[job_id]["error"] = str(e)
                _jobs[job_id]["finished_at"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def update_job(job_id: str, **progress):
    """Merges keys into a running job's `progress` dict — called from
    inside a job's own worker code as it makes progress."""
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["progress"].update(progress)


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def list_jobs(limit: int = 20) -> list[dict]:
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)
        return [dict(j) for j in jobs[:limit]]


def _prune_locked():
    """Caller must hold _lock. Keeps only the most recent JOB_HISTORY_LIMIT jobs."""
    if len(_jobs) <= JOB_HISTORY_LIMIT:
        return
    oldest = sorted(_jobs.values(), key=lambda j: j["started_at"])[: len(_jobs) - JOB_HISTORY_LIMIT]
    for j in oldest:
        _jobs.pop(j["id"], None)

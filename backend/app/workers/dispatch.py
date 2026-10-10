"""Send a job to the configured backend: Celery (Redis) or a background thread of the API process."""

from __future__ import annotations

import threading

from fastapi import BackgroundTasks

from app.config import get_settings
from app.workers.jobs import JOBS


def enqueue(job: str, row_id: int, background: BackgroundTasks | None = None) -> None:
    if get_settings().job_backend == "celery":
        from app.workers.celery_app import TASKS

        TASKS[job].delay(row_id)
    elif background is not None:
        background.add_task(JOBS[job], row_id)
    else:
        threading.Thread(target=JOBS[job], args=(row_id,), daemon=True, name=f"{job}-{row_id}").start()

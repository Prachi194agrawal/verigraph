"""Celery wiring (JOB_BACKEND=celery). Start a worker with:

    celery -A app.workers.celery_app:celery_app worker --loglevel=INFO
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from app import db
from app.config import get_settings
from app.graph.store import get_store
from app.workers.jobs import run_build, run_dataset, run_experiment

_settings = get_settings()
celery_app = Celery("verigraph", broker=_settings.redis_url, backend=_settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,  # a crashed worker's job is redelivered
    worker_prefetch_multiplier=1,  # builds and experiments are long; take one at a time
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)


@celery_app.on_after_configure.connect
def _setup(**_kwargs) -> None:
    db.init_db()


@worker_process_init.connect
def _fresh_connections(**_kwargs) -> None:
    """Prefork children must not reuse sockets opened by the parent: two processes on one Postgres
    connection corrupt the protocol. Drop the inherited pool (without closing the parent's sockets)."""
    if db._engine is not None:
        db._engine.dispose(close=False)
    get_store.cache_clear()


@celery_app.task(name="verigraph.build")
def build_task(build_id: int) -> None:
    run_build(build_id)


@celery_app.task(name="verigraph.dataset")
def dataset_task(dataset_id: int) -> None:
    run_dataset(dataset_id)


@celery_app.task(name="verigraph.experiment")
def experiment_task(experiment_id: int) -> None:
    run_experiment(experiment_id)


TASKS = {"build": build_task, "dataset": dataset_task, "experiment": experiment_task}

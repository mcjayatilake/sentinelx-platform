"""Celery application factory.

Run a worker with:
    celery -A app.workers.celery_app worker --loglevel=INFO
Run the scheduler with:
    celery -A app.workers.celery_app beat --loglevel=INFO
"""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "sentinelx",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks.scan_tasks", "app.workers.tasks.outbox_dispatcher"],
)

celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_max_tasks_per_child=200,
    beat_schedule={
        "dispatch-pending-scan-outbox": {
            "task": "app.workers.tasks.outbox_dispatcher.dispatch_pending_outbox",
            "schedule": settings.scan_outbox_dispatch_interval_seconds,
        },
    },
)

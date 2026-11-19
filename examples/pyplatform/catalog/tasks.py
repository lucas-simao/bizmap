"""Outbound tasks from the catalog service."""

from celery import Celery
from google.cloud import tasks_v2

celery_app = Celery("catalog")


@celery_app.task
def refresh_orders():
    """Rebuild derived order totals on a worker."""
    return True


def enqueue_ingest(order_id: str) -> None:
    """Dispatch a Cloud Task to the worker HTTP ingest endpoint."""
    client = tasks_v2.CloudTasksClient()
    client.create_task(
        parent="projects/demo/locations/us/queues/ingest",
        task={
            "http_request": {
                "http_method": "POST",
                "url": "http://worker.internal/ingest",
            }
        },
    )

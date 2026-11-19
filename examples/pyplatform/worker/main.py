"""Firebase Functions worker for jobs and notifications."""

from firebase_admin import firestore, initialize_app
from firebase_functions import firestore_fn, https_fn, pubsub_fn, scheduler_fn, tasks_fn

from .collections import JOBS, NOTIFICATIONS

initialize_app()
db = firestore.client()


@https_fn.on_request()
def ingest(req):
    """Accept an ingest HTTP call and write a job document."""
    db.collection(JOBS).document("job-1").set({"status": "queued"})
    return {"ok": True}


@https_fn.on_call()
def ping(req):
    return {"pong": True}


@firestore_fn.on_document_created(document="jobs/{id}")
def on_job_created(event):
    """Fan out a notification when a job is created."""
    db.collection(NOTIFICATIONS).add({"job": event.data, "kind": "created"})
    db.collection(JOBS).document(event.id).collection("events").add({"at": "now"})


@firestore_fn.on_document_updated(document="jobs/{id}")
def on_job_updated(event):
    db.collection(NOTIFICATIONS).document("last").set({"job": event.data})


@scheduler_fn.on_schedule(schedule="every 15 minutes")
def sweep(event):
    """Expire stale jobs."""
    for snap in db.collection(JOBS).where("status", "==", "queued").stream():
        snap.reference.update({"status": "expired"})


@tasks_fn.on_task_dispatched()
def handle_task(req):
    db.collection(JOBS).document("from-task").set({"status": "running"})


@pubsub_fn.on_message_published(topic="jobs")
def on_jobs_topic(event):
    db.collection(JOBS).add({"status": "from-pubsub"})

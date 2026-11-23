"""`create_task` is Google Cloud Tasks only with a Cloud Tasks client or its
arguments; asyncio / TaskGroup / event-loop `create_task(coro)` starts a
coroutine in the same process and is not a queue."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python")

from bizmap.scan import scan_path  # noqa: E402

SRC = '''
import asyncio
from google.cloud import tasks_v2


class Cache:
    def warm(self):
        asyncio.create_task(self._load())

    def refresh(self, loop):
        loop.create_task(self._load())

    async def group(self):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._load())

    def later(self):
        self._tasks.create_task(self._load())

    async def _load(self):
        return 1


def enqueue_kw(client):
    client.create_task(parent="projects/p/locations/l/queues/ingest", task={"http_request": {"url": "https://w/x"}})


def enqueue_request(client):
    client.create_task(request={"parent": "q", "task": {}})


def enqueue_positional(client, parent, task):
    client.create_task(parent, task)


def enqueue_client():
    tasks_v2.CloudTasksClient().create_task(task={"http_request": {"http_method": "POST", "url": "https://w/y"}})
'''


def test_only_cloud_tasks_calls_enqueue(tmp_path: Path) -> None:
    (tmp_path / "jobs.py").write_text(SRC, encoding="utf-8")
    graph = scan_path(tmp_path)
    producers = sorted(
        e.from_id.split("function:", 1)[1]
        for e in graph.edges.values()
        if e.type == "enqueues" and "cloud_tasks" in e.to_id
    )
    assert producers == ["jobs.enqueue_client", "jobs.enqueue_kw", "jobs.enqueue_positional", "jobs.enqueue_request"]

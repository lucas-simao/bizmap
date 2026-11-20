"""Progress and per-phase timings on stderr for slow scans.

Nothing is printed for a scan that finishes within ``threshold`` seconds. Once
a scan runs longer, the lines seen so far are printed, then each phase as it
starts (``js/ts: 19 files…``) and ends, and a heartbeat (``still in js/ts``)
every ``heartbeat`` seconds while one phase runs long. Only stderr is used,
so the graph JSON and stdout stay deterministic.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO

_current: Progress | None = None


def phase_kind(label: str) -> str:
    """`shop: files (0 go, 1 py)` → `files`; `merge + links (2 repos)` → `merge + links`."""
    kind = label.split(":", 1)[-1].strip()
    return re.sub(r"\s*\(.*\)$", "", kind) or kind


class Progress:
    def __init__(
        self, stream: IO[str] | None = None, threshold: float = 3.0, heartbeat: float = 5.0
    ) -> None:
        self.stream = stream or sys.stderr
        self.threshold = threshold
        self.heartbeat = heartbeat
        self.start = time.perf_counter()
        self.pending: list[str] = []
        self.live = False
        self.phases: list[tuple[str, float]] = []
        self.current: tuple[str, float] | None = None  # phase running now, its start
        self.last_line = self.start
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def elapsed(self) -> float:
        return time.perf_counter() - self.start

    def _print(self, line: str) -> None:
        print(line, file=self.stream, flush=True)
        self.last_line = time.perf_counter()

    def _go_live(self) -> None:
        self.live = True
        for item in self.pending:
            self._print(item)
        self.pending.clear()

    def emit(self, message: str) -> None:
        with self._lock:
            line = f"[bizmap {self.elapsed():6.1f}s] {message}"
            if self.live:
                self._print(line)
                return
            self.pending.append(line)
            if self.elapsed() >= self.threshold:
                self._go_live()

    def begin(self, label: str, files: int | None = None) -> None:
        with self._lock:
            self.current = (label, time.perf_counter())
        what = f"{files} file{'s' if files != 1 else ''}…" if files is not None else "…"
        self.emit(f"{label}: {what}" if files is not None else f"{label}…")

    def end(self, label: str, seconds: float) -> None:
        with self._lock:
            self.phases.append((label, seconds))
            self.current = None
        self.emit(f"{label} {seconds:.1f}s")

    def tick(self) -> None:
        """Called by the heartbeat thread: go live once past the threshold, and
        say which phase is still running when nothing was printed for a while."""
        with self._lock:
            now = time.perf_counter()
            if not self.live:
                if now - self.start < self.threshold:
                    return
                self._go_live()
            if self.current and now - self.last_line >= self.heartbeat:
                label, t0 = self.current
                self._print(f"[bizmap {now - self.start:6.1f}s] still in {label} ({now - t0:.0f}s)")

    def run_heartbeat(self, interval: float = 0.5) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                self.tick()

        self._thread = threading.Thread(target=loop, name="bizmap-progress", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def summary(self) -> None:
        with self._lock:
            if not self.live and self.elapsed() < self.threshold:
                return
            if not self.live:
                self._go_live()
            totals: dict[str, float] = {}
            for label, seconds in self.phases:
                kind = phase_kind(label)
                totals[kind] = totals.get(kind, 0.0) + seconds
            parts = ", ".join(
                f"{k} {v:.1f}s" for k, v in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            self._print(f"[bizmap] done in {self.elapsed():.1f}s ({parts})")


def start(
    threshold: float = 3.0, stream: IO[str] | None = None, heartbeat: float = 5.0, live: bool = True
) -> Progress:
    """Start reporting. ``live`` runs the background heartbeat (tests turn it off)."""
    global _current
    stop_silently()
    _current = Progress(stream=stream, threshold=threshold, heartbeat=heartbeat)
    if live:
        _current.run_heartbeat()
    return _current


def stop_silently() -> None:
    global _current
    if _current is not None:
        _current.close()
    _current = None


def stop() -> None:
    global _current
    if _current is not None:
        _current.close()
        _current.summary()
    _current = None


def note(message: str) -> None:
    if _current is not None:
        _current.emit(message)


def status(message: str, every: float = 2.0) -> None:
    """A line from inside a long phase (``go: 1200/3700 files``): printed only
    once the scan is slow, at most every ``every`` seconds, never queued."""
    cur = _current
    if cur is None or not cur.live:
        return
    with cur._lock:
        now = time.perf_counter()
        if now - cur.last_line < every:
            return
        cur._print(f"[bizmap {now - cur.start:6.1f}s] {message}")


def begin(label: str, files: int | None = None) -> None:
    """A phase starts (shown only once the scan is slow)."""
    if _current is not None:
        _current.begin(label, files)


@contextmanager
def phase(label: str) -> Iterator[None]:
    """Time a phase (``"shop-api: go"``); reported when the scan is slow."""
    if _current is None:
        yield
        return
    t0 = time.perf_counter()
    _current.begin(label)
    try:
        yield
    finally:
        if _current is not None:
            _current.end(label, time.perf_counter() - t0)


def done(label: str, t0: float) -> None:
    """Record a phase that started at ``t0`` (``time.perf_counter()``)."""
    if _current is None:
        return
    _current.end(label, time.perf_counter() - t0)

"""Progress and per-phase timings on stderr for slow scans.

Nothing is printed for a scan that finishes within ``threshold`` seconds; once
a scan runs longer, the phases seen so far are printed and later ones as they
end. Only stderr is used, so the graph JSON and stdout stay deterministic.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO

_current: Progress | None = None


class Progress:
    def __init__(self, stream: IO[str] | None = None, threshold: float = 3.0) -> None:
        self.stream = stream or sys.stderr
        self.threshold = threshold
        self.start = time.perf_counter()
        self.pending: list[str] = []
        self.live = False
        self.phases: list[tuple[str, float]] = []

    def elapsed(self) -> float:
        return time.perf_counter() - self.start

    def emit(self, message: str) -> None:
        line = f"[bizmap {self.elapsed():6.1f}s] {message}"
        if self.live:
            print(line, file=self.stream, flush=True)
            return
        self.pending.append(line)
        if self.elapsed() >= self.threshold:
            self.live = True
            for item in self.pending:
                print(item, file=self.stream, flush=True)
            self.pending.clear()

    def summary(self) -> None:
        if not self.live and self.elapsed() < self.threshold:
            return
        if not self.live:
            for item in self.pending:
                print(item, file=self.stream, flush=True)
        totals: dict[str, float] = {}
        for label, seconds in self.phases:
            kind = label.split(":", 1)[-1].strip()
            totals[kind] = totals.get(kind, 0.0) + seconds
        parts = ", ".join(f"{k} {v:.1f}s" for k, v in sorted(totals.items(), key=lambda kv: -kv[1]))
        print(f"[bizmap] done in {self.elapsed():.1f}s ({parts})", file=self.stream, flush=True)


def start(threshold: float = 3.0, stream: IO[str] | None = None) -> Progress:
    global _current
    _current = Progress(stream=stream, threshold=threshold)
    return _current


def stop() -> None:
    global _current
    if _current is not None:
        _current.summary()
    _current = None


def note(message: str) -> None:
    if _current is not None:
        _current.emit(message)


@contextmanager
def phase(label: str) -> Iterator[None]:
    """Time a phase (``"shop-api: go"``); reported when the scan is slow."""
    if _current is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        seconds = time.perf_counter() - t0
        _current.phases.append((label, seconds))
        _current.emit(f"{label} {seconds:.1f}s")


def done(label: str, t0: float) -> None:
    """Record a phase that started at ``t0`` (``time.perf_counter()``)."""
    if _current is None:
        return
    seconds = time.perf_counter() - t0
    _current.phases.append((label, seconds))
    _current.emit(f"{label} {seconds:.1f}s")

from __future__ import annotations

from pathlib import Path

from bizmap.detect.go_lang import GoAnalyzer
from bizmap.detect.rules import load_rules


def test_go_statement_forms(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module jobtest\n\ngo 1.22\n", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "jobs.go").write_text(
        """
package jobs

import (
    "context"
    "time"
)

type Ticker struct{}

func (t *Ticker) Run(ctx context.Context) {
    tk := time.NewTicker(time.Second)
    <-tk.C
}

func Start(ctx context.Context) {
    <-time.Tick(time.Second)
}
""",
        encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "context"
    "time"

    "jobtest/jobs"
)

func main() {
    ctx := context.Background()
    t := jobs.Ticker{}
    go t.Run(ctx)
    go jobs.Start(ctx)
    go runLoop(ctx)
    go (&jobs.Ticker{}).Run(ctx)
    go func() {
        tk := time.NewTicker(time.Second)
        <-tk.C
    }()
    go func() {
        for {
            select {
            case <-ctx.Done():
                return
            case <-time.After(time.Second):
                return
            }
        }
    }()
}

func runLoop(ctx context.Context) {
    tk := time.NewTicker(time.Second)
    <-tk.C
}
""",
        encoding="utf-8",
    )
    files = [tmp_path / "main.go", jobs_dir / "jobs.go"]
    analysis = GoAnalyzer(load_rules()).analyze(files, tmp_path)
    symbols = {j.handler.qualname for j in analysis.jobs}
    assert any("Ticker.Run" in s for s in symbols), symbols
    assert any(s.endswith(".Start") or s.endswith("Start") for s in symbols), symbols
    assert any(s.endswith("runLoop") for s in symbols), symbols
    assert any("go.func" in s for s in symbols), symbols
    assert len(analysis.jobs) >= 5, symbols

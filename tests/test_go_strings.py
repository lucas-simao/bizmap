from __future__ import annotations

from pathlib import Path

from bizmap.detect.go_lang import GoAnalyzer
from bizmap.detect.rules import load_rules


def test_const_concat_and_dynamic_sql(tmp_path: Path) -> None:
    src = tmp_path / "repo.go"
    src.write_text(
        """
package repository

import "github.com/jackc/pgx/v5"

const base = `SELECT id, email FROM users`
const filt = ` WHERE status = 'active'`

func (r *Repo) List(ctx context.Context) error {
    q := base + filt
    _, err := r.tx.Query(ctx, q)
    return err
}

func (r *Repo) Patch(ctx context.Context, col string, val any) error {
    q := "UPDATE users SET " + col + " = $1"
    _, err := r.tx.Exec(ctx, q, val)
    return err
}
""",
        encoding="utf-8",
    )
    analysis = GoAnalyzer(load_rules()).analyze([src], tmp_path)
    static = [s for s in analysis.sql_sites if "SELECT id" in s.sql]
    assert static
    assert static[0].fully_static
    dyn = [s for s in analysis.sql_sites if s.dynamic]
    assert dyn
    assert "UPDATE users SET" in dyn[0].sql

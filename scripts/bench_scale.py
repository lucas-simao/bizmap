"""Scale benchmark: a synthetic Go service with a long call chain and many
inline-closure routes. Prints scan time per size (and the top hotspots with
--profile) so non-linear growth shows up.

    python scripts/bench_scale.py 500:150 1000:300 2000:600 [--profile]
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
import tempfile
import time
from pathlib import Path

from bizmap.scan import scan_path


def build(root: Path, n_fns: int, n_routes: int) -> None:
    (root / "go.mod").write_text("module bench\n\ngo 1.22\n", encoding="utf-8")
    (root / "schema.sql").write_text("CREATE TABLE orders (id int primary key, status text);\n", encoding="utf-8")
    chain = ["package main\n", 'import "database/sql"\n', "var db *sql.DB\n"]
    for i in range(n_fns):
        nxt = f"\tf{i + 1}()\n" if i + 1 < n_fns else '\tdb.Query("SELECT id, status FROM orders")\n'
        chain.append(f"func f{i}() {{\n{nxt}}}\n")
    (root / "chain.go").write_text("".join(chain), encoding="utf-8")
    routes = ["package main\n", 'import "github.com/labstack/echo/v4"\n', "func main() {\n\te := echo.New()\n"]
    for r in range(n_routes):
        target = (r * 7) % n_fns
        routes.append(f'\te.GET("/r{r}", func(c echo.Context) error {{\n\t\tf{target}()\n\t\treturn nil\n\t}})\n')
    routes.append("}\n")
    (root / "main.go").write_text("".join(routes), encoding="utf-8")


def main(argv: list[str]) -> None:
    profile = "--profile" in argv
    sizes = [a for a in argv if ":" in a] or ["500:150", "1000:300", "2000:600"]
    for spec in sizes:
        n_fns, n_routes = (int(x) for x in spec.split(":"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, n_fns, n_routes)
            prof = cProfile.Profile() if profile else None
            t0 = time.perf_counter()
            if prof:
                prof.enable()
            graph = scan_path(root)
            if prof:
                prof.disable()
            dt = time.perf_counter() - t0
            print(f"fns={n_fns} routes={n_routes} nodes={len(graph.nodes)} edges={len(graph.edges)} scan={dt:.2f}s")
            if prof:
                buf = io.StringIO()
                pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(12)
                print("\n".join(line for line in buf.getvalue().splitlines() if line.strip())[:4000])


if __name__ == "__main__":
    main(sys.argv[1:])

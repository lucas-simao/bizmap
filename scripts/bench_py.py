"""Synthetic FastAPI monorepo benchmark: how the Python phase scales.

    python scripts/bench_py.py 150 300 600        # modules
    python scripts/bench_py.py 300 --profile       # cProfile top 30
    python scripts/bench_py.py 40 --dump g.json    # write the graph (for diffs)

Each module has 12 `from … import`, an APIRouter with routes, a service
class whose methods run UPDATEs, 25 functions running SELECTs, `obj.method()`
calls and calls into other modules; `db/schema.sql` has 300 tables. Names are
neutral (orders, carts…).
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import sys
import tempfile
import time
from pathlib import Path

TABLES = 300


def _w(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def module(i: int, n: int) -> str:
    deps = [(i + k) % n for k in range(1, 13)]
    imports = "\n".join(f"from app.mod{d}.core import query{d}_0, Service{d}" for d in deps)
    t = lambda k: f"t{(i * 7 + k) % TABLES}"  # noqa: E731
    funcs = "\n\n".join(
        f'''def query{i}_{k}(conn, item_id: int):
    """Read {t(k)} rows."""
    rows = conn.execute("SELECT id, status, total FROM {t(k)} WHERE id = %s", (item_id,))
    if k_flag({k}):
        return [r for r in rows if r]
    return list(rows)'''
        for k in range(25)
    )
    return f'''from fastapi import APIRouter, Depends

{imports}

router = APIRouter(prefix="/orders{i}")


def k_flag(k: int) -> bool:
    return k % 2 == 0


class Service{i}:
    def __init__(self, conn):
        self.conn = conn

    def pay(self, item_id: int) -> None:
        self.conn.execute("UPDATE {t(1)} SET status = 'paid' WHERE id = %s", (item_id,))

    def cancel(self, item_id: int) -> None:
        self.conn.execute("UPDATE {t(2)} SET status = 'cancelled' WHERE id = %s", (item_id,))

    def total(self, item_id: int) -> int:
        rows = query{i}_0(self.conn, item_id)
        return len(rows)


{funcs}


def get_conn():
    return None


@router.get("/{{item_id}}")
def read(item_id: int, conn=Depends(get_conn)):
    svc = Service{i}(conn)
    other = Service{deps[0]}(conn)
    other.total(item_id)
    query{deps[1]}_0(conn, item_id)
    return {{"total": svc.total(item_id)}}


@router.post("/{{item_id}}/pay")
def pay(item_id: int, conn=Depends(get_conn)):
    svc = Service{i}(conn)
    svc.pay(item_id)
    return {{"ok": True}}
'''


def build(root: Path, modules: int) -> int:
    _w(root, "pyproject.toml", '[project]\nname = "shop"\nversion = "0"\n')
    _w(root, "db/schema.sql", "".join(
        f"CREATE TABLE t{k} (id INT PRIMARY KEY, status TEXT, total INT);\n" for k in range(TABLES)
    ))
    _w(root, "app/__init__.py", "")
    mains = []
    for i in range(modules):
        _w(root, f"app/mod{i}/__init__.py", "")
        _w(root, f"app/mod{i}/core.py", module(i, modules))
        mains.append(f"from app.mod{i}.core import router as router{i}")
    _w(root, "app/main.py", "from fastapi import FastAPI\n" + "\n".join(mains) + "\n\napp = FastAPI()\n"
       + "".join(f"app.include_router(router{i})\n" for i in range(modules)))
    return modules


def run(n: int, profile: bool, dump: Path | None) -> None:
    from bizmap.scan import scan_path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "shop"  # fixed name: the repo name is in every id
        build(root, n)
        pr = cProfile.Profile() if profile else None
        t0 = time.perf_counter()
        if pr:
            pr.enable()
        graph = scan_path(root)
        if pr:
            pr.disable()
        dt = time.perf_counter() - t0
        print(f"modules={n} nodes={len(graph.nodes)} edges={len(graph.edges)} scan={dt:.2f}s", flush=True)
        if dump is not None:
            data = graph.to_dict()
            data.pop("generated_at", None)
            dump.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        if pr:
            out = io.StringIO()
            pstats.Stats(pr, stream=out).sort_stats("tottime").print_stats(30)
            print(out.getvalue())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sizes", nargs="*", type=int)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--dump", type=Path, help="write the graph JSON (one size only)")
    args = ap.parse_args(argv)
    for n in args.sizes or [150, 300]:
        run(n, args.profile, args.dump)
    return 0


if __name__ == "__main__":
    sys.exit(main())

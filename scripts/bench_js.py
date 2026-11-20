"""Synthetic React/TS front-end benchmark: how the JS/TS phase scales.

    python scripts/bench_js.py 1000 2000 4000      # files (approx.)
    python scripts/bench_js.py 2000 --profile      # cProfile top 30

Each domain has an api module (axios instance + wrappers), hooks, utils
and components importing each other. Names are neutral (orders, carts…).
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import sys
import tempfile
import time
from pathlib import Path


def _w(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def domain(root: Path, d: int) -> int:
    name = f"orders{d}"
    base = f"src/features/{name}"
    _w(root, f"{base}/api.ts", f"""import {{ http }} from '../../lib/http';

export interface Order{d} {{ id: string; status: string; total: number }}

export async function list{d}(page: number): Promise<Order{d}[]> {{
  const {{ data }} = await http.get(`/api/v1/{name}?page=${{page}}`);
  return data;
}}

export async function get{d}(id: string): Promise<Order{d}> {{
  const {{ data }} = await http.get(`/api/v1/{name}/${{id}}`);
  return data;
}}

export async function pay{d}(id: string): Promise<void> {{
  await http.post(`/api/v1/{name}/${{id}}/pay`, {{ at: Date.now() }});
}}
""")
    _w(root, f"{base}/hooks.ts", f"""import {{ useEffect, useState }} from 'react';
import {{ get{d}, list{d}, Order{d} }} from './api';
import {{ formatTotal{d} }} from './utils';

export function useOrders{d}(page: number) {{
  const [rows, setRows] = useState<Order{d}[]>([]);
  useEffect(() => {{
    list{d}(page).then((r) => setRows(r.map((o) => ({{ ...o, label: formatTotal{d}(o.total) }}))));
  }}, [page]);
  return rows;
}}

export function useOrder{d}(id: string) {{
  const [row, setRow] = useState<Order{d} | null>(null);
  useEffect(() => {{
    get{d}(id).then(setRow);
  }}, [id]);
  return row;
}}
""")
    _w(root, f"{base}/utils.ts", f"""export function formatTotal{d}(cents: number): string {{
  const value = (cents / 100).toFixed(2);
  if (cents < 0) {{
    return `-${{value}}`;
  }} else if (cents === 0) {{
    return 'free';
  }}
  return `$${{value}}`;
}}

export const statusLabel{d} = (s: string) => (s === 'paid' ? 'Paid' : s === 'new' ? 'New' : s);
""")
    for i in range(3):
        _w(root, f"{base}/components/Row{i}.tsx", f"""import React from 'react';
import {{ pay{d} }} from '../api';
import {{ statusLabel{d}, formatTotal{d} }} from '../utils';
import {{ useOrder{d} }} from '../hooks';

export function Row{d}_{i}({{ id }}: {{ id: string }}) {{
  const order = useOrder{d}(id);
  if (!order) return null;
  const onPay = async () => {{
    await pay{d}(order.id);
  }};
  return (
    <div className="row">
      <span>{{statusLabel{d}(order.status)}}</span>
      <span>{{formatTotal{d}(order.total)}}</span>
      <button onClick={{onPay}}>Pay</button>
    </div>
  );
}}
""")
    return 6


def build(root: Path, files: int) -> int:
    _w(root, "package.json", '{"name": "shop-web"}\n')
    _w(root, "src/lib/http.ts", """import axios from 'axios';

export const http = axios.create({ baseURL: import.meta.env.VITE_API_URL });
""")
    made, d = 1, 0
    while made < files:
        made += domain(root, d)
        d += 1
    return made


def run(n: int, profile: bool) -> None:
    from bizmap.scan import scan_path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        made = build(root, n)
        pr = cProfile.Profile() if profile else None
        t0 = time.perf_counter()
        if pr:
            pr.enable()
        graph = scan_path(root)
        if pr:
            pr.disable()
        dt = time.perf_counter() - t0
        print(
            f"files={made} nodes={len(graph.nodes)} edges={len(graph.edges)} scan={dt:.2f}s "
            f"({1000 * dt / max(made, 1):.1f} ms/file)",
            flush=True,
        )
        if pr:
            out = io.StringIO()
            pstats.Stats(pr, stream=out).sort_stats("tottime").print_stats(30)
            pstats.Stats(pr, stream=out).sort_stats("cumulative").print_stats(40)
            print(out.getvalue())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sizes", nargs="*", type=int)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args(argv)
    for n in args.sizes or [1000, 2000]:
        run(n, args.profile)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""TEMP (removed before review): dump graphs and the golden summary with
whatever bizmap is importable."""
import json, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import bench_py
from bizmap.scan import scan_path
from bizmap.snapshot import summarize
tag = sys.argv[1]
for n in (30, 40, 120):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "shop"
        bench_py.build(root, n)
        t0 = time.perf_counter()
        data = scan_path(root).to_dict()
        print(tag, n, f"{time.perf_counter() - t0:.1f}s", flush=True)
        data.pop("generated_at", None); data.pop("version", None)
        Path(f"/tmp/{tag}_{n}.json").write_text(json.dumps(data, sort_keys=True, indent=0))
        if n == 30:
            Path(f"/tmp/{tag}_golden.txt").write_text(summarize(scan_path(root).to_dict()))

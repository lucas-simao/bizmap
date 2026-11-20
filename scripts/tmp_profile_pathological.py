import cProfile, pstats, sys, tempfile, time, io
from pathlib import Path
from bizmap.scan import scan_path

CASES = {
    "js_elseif_800": ("a.js", "function f(x, db) {\n  if (x === 0) { db.query('UPDATE orders SET status = 1'); }\n" + "".join(f"  else if (x === {i}) {{ y = {i}; }}\n" for i in range(1, 800)) + "}\n"),
    "py_const_10000": ("a.py", "Q = " + " + ".join(["'UPDATE orders SET status = 1 '"] + ["' '"] * 9999) + "\n\ndef f(db):\n    db.execute(Q)\n"),
    "py_chain_3000": ("a.py", "def f(db):\n    return db" + ".q()" * 3000 + "\n"),
}
for name, (fname, body) in CASES.items():
    d = Path(tempfile.mkdtemp())
    (d / fname).write_text(body)
    (d / "schema.sql").write_text("CREATE TABLE orders (id int primary key, status int);\n")
    pr = cProfile.Profile()
    t = time.perf_counter()
    sys.setrecursionlimit(100000)
    pr.enable()
    try:
        scan_path(d)
    except Exception as e:
        print("ERR", e)
    pr.disable()
    print(f"===== {name}: {time.perf_counter()-t:.1f}s")
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(18)
    print("\n".join(s.getvalue().splitlines()[:45]))

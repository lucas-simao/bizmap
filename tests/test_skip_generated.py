"""Generated code (build output, .yarn, minified bundles) and .gitignore'd
paths are left out of the scan by default, and can be brought back."""

from __future__ import annotations

from pathlib import Path

from bizmap.files import GitIgnore, WalkOptions, iter_files, looks_generated
from bizmap.scan import scan_path

JS = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]
SOURCE = "export function listOrders(db: any) { return db.query('SELECT id FROM orders'); }\n"


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _rels(root: Path, files: list[Path]) -> list[str]:
    return [p.relative_to(root.resolve()).as_posix() for p in files]


def _project(root: Path) -> None:
    _write(root, "src/orders.ts", SOURCE)
    _write(root, ".yarn/releases/yarn-4.1.0.cjs", "module.exports = function(){};\n")
    _write(root, ".yarn/cache/pkg/index.js", "module.exports = 1;\n")
    for d in ("dist", "build", "out", "coverage", ".next", ".turbo"):
        _write(root, f"{d}/app.js", "var a = 1;\n")
    _write(root, "public/vendor.min.js", "var a=1;\n")
    _write(root, "public/app.bundle.js", "var a=1;\n")
    _write(root, "public/compiled.js", "var a = 1;\n//# sourceMappingURL=compiled.js.map\n")
    _write(root, "public/packed.js", "var " + ",".join(f"a{i}={i}" for i in range(800)) + ";\n")


def test_generated_paths_are_skipped_by_default(tmp_path: Path) -> None:
    _project(tmp_path)
    opts = WalkOptions()
    files = _rels(tmp_path, iter_files(tmp_path, JS, options=opts))
    assert files == ["src/orders.ts"], files
    assert opts.stats == {"generated_dirs": 5, "generated_files": 4}, opts.stats


def test_include_brings_generated_paths_back(tmp_path: Path) -> None:
    _project(tmp_path)
    files = _rels(tmp_path, iter_files(tmp_path, JS, options=WalkOptions(include=[".yarn", "public/packed.js"])))
    assert ".yarn/cache/pkg/index.js" in files and "public/packed.js" in files
    assert "dist/app.js" not in files and "public/vendor.min.js" not in files
    everything = _rels(tmp_path, iter_files(tmp_path, JS, options=WalkOptions(skip_generated=False)))
    assert "out/app.js" in everything and "public/compiled.js" in everything
    assert "dist/app.js" not in everything  # dist/build/node_modules stay skipped unless included


def test_minified_heuristic() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        normal = _write(root, "a.js", "\n".join(f"const x{i} = {i};" for i in range(500)) + "\n")
        long_line = _write(root, "b.js", "x=" + "1+" * 1500 + "1;\n")
        mapped = _write(root, "c.js", "x = 1;\n//# sourceMappingURL=c.js.map\n")
        ts = _write(root, "d.ts", "x=" + "1+" * 1500 + "1;\n")
        assert not looks_generated(normal)
        assert looks_generated(long_line) and looks_generated(mapped)
        assert not looks_generated(ts)  # only JS bundles are checked


def test_gitignore_rules(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write(tmp_path, ".gitignore", "generated/\n*.gen.ts\n/scratch\n!keep.gen.ts\n# comment\n")
    _write(tmp_path, "pkg/.gitignore", "local/\n")
    _write(tmp_path, "src/a.ts", SOURCE)
    _write(tmp_path, "src/generated/client.ts", SOURCE)
    _write(tmp_path, "src/types.gen.ts", SOURCE)
    _write(tmp_path, "src/keep.gen.ts", SOURCE)
    _write(tmp_path, "scratch/x.ts", SOURCE)
    _write(tmp_path, "nested/scratch/y.ts", SOURCE)  # `/scratch` is anchored to the root
    _write(tmp_path, "pkg/local/z.ts", SOURCE)
    _write(tmp_path, "pkg/b.ts", SOURCE)
    opts = WalkOptions()
    files = _rels(tmp_path, iter_files(tmp_path, JS, options=opts))
    assert files == ["nested/scratch/y.ts", "pkg/b.ts", "src/a.ts", "src/keep.gen.ts"], files
    off = _rels(tmp_path, iter_files(tmp_path, JS, options=WalkOptions(gitignore=False)))
    assert "src/generated/client.ts" in off and "scratch/x.ts" in off


def test_gitignore_above_a_scanned_subdirectory(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write(tmp_path, ".gitignore", "web/legacy/\n")
    _write(tmp_path, "web/src/a.ts", SOURCE)
    _write(tmp_path, "web/legacy/b.ts", SOURCE)
    files = _rels(tmp_path / "web", iter_files(tmp_path / "web", JS))
    assert files == ["src/a.ts"], files


def test_glob_double_star() -> None:
    gi = GitIgnore()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        Path(d, ".gitignore").write_text("**/fixtures/**/*.json\ndocs/**\n", encoding="utf-8")
        gi.load(Path(d), "")
    assert gi.ignored("a/fixtures/b/c.json", False)
    assert gi.ignored("fixtures/c.json", False)
    assert gi.ignored("docs/x/y.md", False)
    assert not gi.ignored("src/c.json", False)


def test_scan_skips_generated_and_reports_counts(tmp_path: Path) -> None:
    _project(tmp_path)
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id int primary key);\n")
    graph = scan_path(tmp_path)
    files = {(n.attrs.get("evidence") or {}).get("file") for n in graph.nodes.values()}
    assert not any(f and (f.startswith(".yarn") or f.startswith("public/")) for f in files), files
    assert graph.attrs["skipped"]["generated_files"] == 4
    assert graph.to_dict()["skipped"] == graph.attrs["skipped"]


def test_yaml_include_and_gitignore_off(tmp_path: Path) -> None:
    _project(tmp_path)
    _write(tmp_path, "bizmap.yaml", "scan:\n  include: ['.yarn']\n  skip_generated: true\n")
    graph = scan_path(tmp_path)
    assert graph.attrs["skipped"]["generated_dirs"] == 4  # .yarn is back

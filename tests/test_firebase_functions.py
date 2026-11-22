"""Firebase Functions in JS/TS: entrypoints of every kind, deployed names,
callable callers, batch writes; Go routes mounted through a helper; nested
repos left out of the walk. Fictional repos.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bizmap.detect.go_helpers import scan_helpers
from bizmap.detect.js_firebase import callable_sites, deployed_names
from bizmap.files import WalkOptions, iter_files

FUNCTIONS = {
    "package.json": '{"name": "fns", "main": "lib/index.js", "dependencies": {"firebase-functions": "^6"}}\n',
    "src/index.ts": """import { initializeApp } from 'firebase-admin/app';
initializeApp();
exports.users = require('./users');
exports.billing = require('./billing');
export * from './top';
""",
    "src/users/index.ts": """const mgmt = require('./mgmt');
exports.archiveUser = mgmt.archiveUser;
export { onScoreCreated } from './listeners';
""",
    "src/users/mgmt.ts": """import { https } from 'firebase-functions/v2';
import { getFirestore } from 'firebase-admin/firestore';
const db = getFirestore();
async function markArchived(id: string) { await db.collection('users').doc(id).update({ archived: true }); }
exports.archiveUser = https.onCall(async (req) => {
  await markArchived(req.data.id);
  return { ok: true };
});
""",
    "src/users/listeners.ts": """import { firestore } from 'firebase-functions/v2';
import { getFirestore } from 'firebase-admin/firestore';
const COLLECTION = 'scores';
export const onScoreCreated = firestore.onDocumentCreated({ document: `${COLLECTION}/{id}` }, async (event) => {
  await getFirestore().collection('audit').add({ id: event.params.id });
});
""",
    "src/billing/index.ts": """import { onSchedule } from 'firebase-functions/v2/scheduler';
import { onRequest } from 'firebase-functions/v2/https';
import { onMessagePublished } from 'firebase-functions/v2/pubsub';
import { getFirestore } from 'firebase-admin/firestore';
const db = getFirestore();
exports.syncPrices = onSchedule({ schedule: 'every 30 minutes' }, async () => {
  const snap = await db.collection('companies').get();
  const batch = db.batch();
  snap.docs.forEach((d) => batch.update(db.doc(`companies/${d.id}`), { price: 1 }));
  await batch.commit();
});
exports.webhook = onRequest(async (req, res) => { res.send('ok'); });
exports.onInvoice = onMessagePublished('invoices', async (event) => {
  await db.collection('invoices').add({ id: event.id });
});
""",
    "src/top.ts": """import * as functions from 'firebase-functions/v1';
export const nightly = functions.pubsub.schedule('every day 03:00').onRun(async () => {});
export const legacyCall = functions.https.onCall(async () => ({}));
""",
}

WEB = {
    "package.json": '{"name": "web", "dependencies": {"firebase": "^10"}}\n',
    "src/api.ts": """import { httpsCallable } from 'firebase/functions';
import { functions } from './firebase';
export async function archive(id: string) {
  const fn = httpsCallable<{ id: string }, { ok: boolean }>(
    functions,
    'users-archiveUser',
  );
  return fn({ id });
}
export const missing = httpsCallable(functions, 'users-gone');
""",
}


def _write_all(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _js_texts(files: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in files.items() if k.endswith((".ts", ".js"))}


def test_deployed_names_follow_groups_and_reexports(tmp_path: Path) -> None:
    _write_all(tmp_path, FUNCTIONS)
    names = deployed_names(tmp_path, _js_texts(FUNCTIONS))
    assert names[("src/users/mgmt.ts", "archiveUser")] == "users-archiveUser"
    assert names[("src/users/listeners.ts", "onScoreCreated")] == "users-onScoreCreated"
    assert names[("src/billing/index.ts", "syncPrices")] == "billing-syncPrices"
    assert names[("src/top.ts", "nightly")] == "nightly"  # `export *` adds no group


def test_deployed_names_need_firebase_functions_dependency(tmp_path: Path) -> None:
    files = {**FUNCTIONS, "package.json": '{"name": "x", "main": "lib/index.js"}\n'}
    _write_all(tmp_path, files)
    assert deployed_names(tmp_path, _js_texts(files)) == {}


def test_callable_sites_generic_multiline_and_module_const() -> None:
    sites = callable_sites(WEB, {"src.api.archive": ("src/api.ts", 3)}, {"src.api.archive": 9})
    assert {(s.symbol, s.name) for s in sites} == {
        ("src.api.archive", "users-archiveUser"),
        ("src.api.missing", "users-gone"),
    }


def test_callable_from_url() -> None:
    texts = {"a.ts": "const f = httpsCallableFromURL(fns, 'https://us-central1-p.cloudfunctions.net/users-x');\n"}
    assert [s.name for s in callable_sites(texts, {}, {})] == ["users-x"]


def _scan(tmp_path: Path):  # noqa: ANN202
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_paths

    _write_all(tmp_path / "fns", FUNCTIONS)
    _write_all(tmp_path / "web", WEB)
    return scan_paths([tmp_path / "fns", tmp_path / "web"], base=tmp_path)


def test_scan_functions_entrypoints_and_callable_links(tmp_path: Path) -> None:
    g = _scan(tmp_path)
    eps = {n.attrs.get("deployed_name"): n for n in g.nodes.values() if n.type == "entrypoint"}
    assert eps["users-archiveUser"].attrs["kind"] == "firebase_call"
    assert eps["billing-webhook"].attrs["kind"] == "firebase_http"
    assert eps["billing-syncPrices"].attrs["kind"] == "scheduled_job"
    assert eps["billing-syncPrices"].attrs["schedule"] == "every 30 minutes"
    assert eps["billing-onInvoice"].attrs["kind"] == "pubsub"
    assert eps["billing-onInvoice"].attrs["topic"] == "invoices"
    assert eps["nightly"].attrs["kind"] == "scheduled_job"
    assert eps["legacyCall"].attrs["kind"] == "firebase_call"
    assert eps["users-onScoreCreated"].attrs["kind"] == "firestore"  # namespaced trigger + template path
    assert eps["users-onScoreCreated"].attrs["collection"] == "scores"

    edges = {(e.type, e.from_id, e.to_id) for e in g.edges.values()}
    archive = eps["users-archiveUser"].id
    # the web caller is linked to the callable, across repos
    assert ("calls_http", "web/function:src.api.archive", archive) in edges
    # the handler's calls and data reach the entrypoint
    assert ("calls", "fns/function:src.users.mgmt.archiveUser", "fns/function:src.users.mgmt.markArchived") in edges
    assert ("writes", archive, "fns/collection:users") in edges
    # batch.update(db.doc(`companies/${id}`)) is a write
    assert ("writes", eps["billing-syncPrices"].id, "fns/collection:companies") in edges
    unresolved = [u for u in g.attrs["unresolved_http"] if u["reason"] == "callable_not_scanned"]
    assert [(u["url"], u["from"]) for u in unresolved] == [("users-gone", "web/function:src.api.missing")]


def test_scan_callable_unresolved_when_functions_not_scanned(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_path

    _write_all(tmp_path, WEB)
    g = scan_path(tmp_path)
    names = sorted(u["url"] for u in g.attrs["unresolved_http"] if u["reason"] == "callable_not_scanned")
    assert names == ["users-archiveUser", "users-gone"]


def test_nested_repo_is_not_walked(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.go").write_text("package pkg\n")
    stale = tmp_path / "old"
    (stale / "pkg").mkdir(parents=True)
    (stale / "pkg" / "a.go").write_text("package pkg\n")
    (stale / ".git").write_text("gitdir: ../.git/worktrees/old\n")  # a worktree's `.git` file
    opts = WalkOptions(gitignore=False)
    got = [p.relative_to(tmp_path).as_posix() for p in iter_files(tmp_path, [".go"], options=opts)]
    assert got == ["pkg/a.go"]
    assert opts.stats["nested_repos"] == 1
    # `scan: include` brings it back
    got = iter_files(tmp_path, [".go"], options=WalkOptions(gitignore=False, include=["old"]))
    assert len(got) == 2


GO = {
    "internal/entrypoint/scheduled.go": """package entrypoint

import "github.com/labstack/echo/v4"

const JobsPrefix = "/internal/jobs/"

type ScheduledJob interface {
	Name() string
	Run(ctx context.Context) error
}

type ScheduledJobWithParams interface {
	ScheduledJob
	RunWithParams(ctx context.Context, p url.Values) error
}

type EventHandler interface {
	Path() string
	Handle(ctx context.Context, ev Event) error
}

func RegisterScheduled(e *echo.Echo, auth echo.MiddlewareFunc, job ScheduledJob) {
	e.POST(JobsPrefix+job.Name(), handler(job), auth)
}

func RegisterEvent(e *echo.Echo, auth echo.MiddlewareFunc, h EventHandler) {
	e.POST(h.Path(), eventHandler(h), auth)
}
""",
    "internal/jobs/nightly.go": """package jobs

type Nightly struct{ svc any }

func NewNightly(svc any) *Nightly { return &Nightly{svc: svc} }

func (c *Nightly) Name() string { return "nightly-totals" }

func (c *Nightly) Run(ctx context.Context) error { return nil }
""",
    "internal/consumers/orders.go": """package consumers

const OrdersPath = "/eventarc/firestore/orders"

type Orders struct{}

func (c *Orders) Path() string { return OrdersPath }

func (c *Orders) Handle(ctx context.Context, ev entrypoint.Event) error { return nil }
""",
    "internal/httpapi/routes.go": """package httpapi

import (
	"github.com/labstack/echo/v4"

	"example.com/svc/internal/consumers"
	"example.com/svc/internal/entrypoint"
	"example.com/svc/internal/jobs"
)

func Register(e *echo.Echo) {
	entrypoint.RegisterScheduled(e, auth, jobs.NewNightly(
		svc,
	))
	orders := &consumers.Orders{}
	entrypoint.RegisterEvent(e, auth, orders)
}
""",
}


def test_go_helper_routes() -> None:
    routes = {(r.verb, r.path): r for r in scan_helpers(GO, "example.com/svc")}
    assert set(routes) == {("POST", "/internal/jobs/nightly-totals"), ("POST", "/eventarc/firestore/orders")}
    job = routes[("POST", "/internal/jobs/nightly-totals")]
    assert (job.type_pkg, job.type_name) == ("internal/jobs", "Nightly")
    assert job.handlers == ["Run", "RunWithParams"]
    assert routes[("POST", "/eventarc/firestore/orders")].handlers == ["Handle"]


def test_scan_go_helper_routes_trigger_handlers(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")
    from bizmap.scan import scan_path

    _write_all(tmp_path, {**GO, "go.mod": "module example.com/svc\n\ngo 1.22\n"})
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=False)
    g = scan_path(tmp_path)
    ep = g.nodes.get("entrypoint:http:POST:/internal/jobs/nightly-totals")
    assert ep is not None and ep.attrs["helper"] == "internal/entrypoint.RegisterScheduled"
    targets = {e.to_id for e in g.edges.values() if e.type == "triggers" and e.from_id == ep.id}
    assert "function:internal/jobs.Nightly.Run" in targets
    assert "entrypoint:http:POST:/eventarc/firestore/orders" in g.nodes
    # the route detector's template (`POST /internal/jobs/{job.Name()}`, `e.POST(h.Path(), …)`)
    # is replaced by the expanded routes: no route at the helper's own line
    https = {n.id: n for n in g.nodes.values() if n.type == "entrypoint" and n.attrs.get("kind") == "http"}
    assert set(https) == {"entrypoint:http:POST:/internal/jobs/nightly-totals", "entrypoint:http:POST:/eventarc/firestore/orders"}, sorted(https)
    assert not any(e.from_id not in g.nodes or e.to_id not in g.nodes for e in g.edges.values())

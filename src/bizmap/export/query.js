// BizMap query engine for the exported page: a line-by-line port of
// src/bizmap/query.py (`who-writes`, `who-reads`, `trace`), reading the same
// graph (packed in columns by bizmap/export/home.py:engine_data) and printing
// the same `bizmap.query/v1` JSON. The tests compare both entity by entity:
// a change to query.py must be ported here.
(function (root) {
  "use strict";
  var SCHEMA = "bizmap.query/v1";
  var DATA_TYPES = ["table", "view", "collection"];
  var RANK = { exact: 4, inferred: 3, heuristic: 2, unknown: 1 };
  var BY_RANK = { 4: "exact", 3: "inferred", 2: "heuristic", 1: "unknown" };
  var UP_EDGES = ["calls", "triggers"], DOWN_EDGES = ["calls", "triggers"];
  var MAX_VISIT = 2000, MAX_ROOTS = 25, MAX_WRITER_HOPS = 50;
  var ENGINE = { postgres: "Postgres", mysql: "MySQL", sqlite: "SQLite", sqlserver: "SQL Server" };

  // ── Python semantics ──
  function truthy(v) {
    if (v === null || v === undefined || v === false || v === 0 || v === "") return false;
    if (Array.isArray(v)) return v.length > 0;
    if (typeof v === "object") return Object.keys(v).length > 0;
    return true;
  }
  function or() { for (var i = 0; i < arguments.length; i++) if (truthy(arguments[i])) return arguments[i]; return arguments[arguments.length - 1]; }
  function pyStr(v) {
    if (v === null || v === undefined) return "None";
    if (v === true) return "True";
    if (v === false) return "False";
    return String(v);
  }
  // str.split(sep, max) — the last part keeps the rest
  function psplit(s, sep, max) {
    var parts = s.split(sep);
    if (max === undefined || parts.length <= max + 1) return parts;
    return parts.slice(0, max).concat([parts.slice(max).join(sep)]);
  }
  function last(a) { return a[a.length - 1]; }
  function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }
  function cmpTuple(a, b) { for (var i = 0; i < a.length; i++) { var c = cmp(a[i], b[i]); if (c) return c; } return 0; }
  function sortStr(a) { return a.slice().sort(cmp); }
  function uniqSorted(a) { var s = sortStr(a), out = []; s.forEach(function (x, i) { if (!i || x !== s[i - 1]) out.push(x); }); return out; }
  function rankOf(c) { return RANK[pyStr(or(c, "unknown"))] || 1; }

  function split(nid) {
    var head = psplit(nid, ":", 1)[0];
    if (head.indexOf("/") >= 0) { var p = psplit(nid, "/", 1); return [p[0], p[1]]; }
    return [null, nid];
  }

  // ── the index (Index.from_dict) ──
  function Index(g) {
    this.g = g;
    this.repo = g.repo === undefined ? null : g.repo;
    var n = g.ids.length;
    this.pos = new Map();
    for (var k = 0; k < n; k++) this.pos.set(g.ids[k], k);
    this.ndict = new Array(n);
    var x = g.x || {};
    for (k = 0; k < n; k++) {
      if (g.ty[k] === -1) { this.ndict[k] = null; continue; }
      var d = { type: g.T[g.ty[k]] };
      d.name = g.nm[k] === undefined ? null : g.nm[k];
      if (g.ef[k] >= 0 || g.el[k] !== null) d.evidence = { file: g.ef[k] >= 0 ? g.F[g.ef[k]] : null, line: g.el[k] };
      Object.keys(x).forEach(function (f) { if (Object.prototype.hasOwnProperty.call(x[f], k)) d[f === "uq" ? "unresolved_queues" : f] = x[f][k]; });
      this.ndict[k] = d;
    }
    // edges as objects, in the graph's order
    var E = g.e, ne = E.a.length;
    this.out = new Map(); this.inc = new Map();
    for (var j = 0; j < ne; j++) {
      var e = { _k: j, type: g.ET[E.t[j]], from: g.ids[E.a[j]], to: g.ids[E.b[j]], confidence: g.C[E.c[j]] };
      if (E.d[j]) e.derived = true;
      if (E.f[j] >= 0) e.evidence = { file: g.F[E.f[j]], line: E.l[j] };
      var fx = g.fe[j];
      if (fx) { e.via = [{ path: fx[0].map(function (p) { return g.ids[p]; }) }]; e.column_map = fx[1].map(function (c) { return { from: c[0], to: c[1], confidence: c[2] }; }); }
      push(this.out, e.from, e); push(this.inc, e.to, e);
    }
    this.byName = new Map(); this.columns = new Map(); this.colByName = new Map();
    for (k = 0; k < n; k++) {
      var nd = this.ndict[k];
      if (!nd) continue;
      var nid = g.ids[k], t = nd.type;
      if (DATA_TYPES.indexOf(t) >= 0) {
        var name = pyStr(or(nd.name, last(psplit(split(nid)[1], ":", 1)))).toLowerCase();
        if (name !== "unknown") push(this.byName, name, nid);
      } else if (t === "column") {
        var repo = split(nid)[0];
        var table = pyStr(or(nd.table, "")).toLowerCase();
        push(this.columns, (repo === null ? "\u0000" : repo) + "\u0001" + table, nid);
        push(this.colByName, table + "." + pyStr(or(nd.name, "")).toLowerCase(), nid);
      }
    }
    [this.byName, this.columns, this.colByName].forEach(function (m) { m.forEach(function (v, key) { m.set(key, sortStr(v)); }); });
    this.unresolvedHttp = (g.uh || []).map(function (u) { return { from: u[0], method: u[1], url: u[2], reason: u[3] }; });
    this._up = new Map(); this._roots = new Map(); this._closures = new Map();
  }
  function push(m, k, v) { var l = m.get(k); if (!l) m.set(k, l = []); l.push(v); }
  Index.prototype.node = function (nid) { var k = this.pos.get(nid); return k === undefined ? null : this.ndict[k]; };
  Index.prototype.has = function (nid) { return !!this.node(nid); };
  Index.prototype.outOf = function (nid) { return this.out.get(nid) || []; };
  Index.prototype.incOf = function (nid) { return this.inc.get(nid) || []; };
  Index.prototype.up = function (nid) {
    var got = this._up.get(nid);
    if (!got) {
      got = this.incOf(nid).filter(function (e) { return UP_EDGES.indexOf(e.type) >= 0 && !e.derived; })
        .map(function (e) { return [e.from, rankOf(e.confidence)]; });
      this._up.set(nid, got);
    }
    return got;
  };
  Index.prototype.kind = function (nid) {
    var n = this.node(nid) || {};
    return pyStr(or(n.type, psplit(split(nid)[1], ":", 1)[0]));
  };
  Index.prototype.repoOf = function (nid) {
    var n = this.node(nid) || {};
    var v = or(split(nid)[0], n.repo, this.repo);
    return v === undefined ? null : v;
  };
  Index.prototype.store = function (nid) {
    var n = this.node(nid) || {};
    if (n.type === "column") { var t = this.tableOfColumn(nid); return t ? this.store(t) : "SQL"; }
    var db = pyStr(or(n.db, "")).toLowerCase();
    if (n.type === "collection" || db.indexOf("firestore") === 0) return "Firestore";
    var eng = pyStr(or(n.engine, ""));
    if (Object.prototype.hasOwnProperty.call(ENGINE, eng)) return ENGINE[eng];
    var keys = [["postgres", "Postgres"], ["pg", "Postgres"], ["mysql", "MySQL"], ["sqlite", "SQLite"]];
    var parts = db.replace(/-/g, "_").split("_");
    for (var i = 0; i < keys.length; i++) if (parts.indexOf(keys[i][0]) >= 0 || db.indexOf(keys[i][0]) === 0) return keys[i][1];
    return "SQL";
  };
  Index.prototype.tableOfColumn = function (cid) {
    var n = this.node(cid) || {}, repo = split(cid)[0], name = pyStr(or(n.table, ""));
    var kinds = ["table", "view", "collection"];
    for (var i = 0; i < kinds.length; i++) {
      var tid = repo ? repo + "/" + kinds[i] + ":" + name : kinds[i] + ":" + name;
      if (this.has(tid)) return tid;
    }
    return null;
  };
  Index.prototype.label = function (nid) {
    var n = this.node(nid), leaf = split(nid)[1];
    if (!n) return leaf;
    var t = n.type;
    if (t === "entrypoint") {
      var kind = n.kind;
      if (kind === "http") return (pyStr(or(n.method, "ANY")) + " " + pyStr(or(n.path, ""))).trim();
      var sym = or(n.topic, n.path, n.symbol, n.handler, last(psplit(leaf, ":", 2)));
      return (pyStr(kind) + " " + pyStr(sym)).trim();
    }
    if (t === "function") return pyStr(or(n.name, leaf.indexOf("function:") === 0 ? leaf.slice(9) : leaf));
    if (t === "column") return pyStr(n.table) + "." + pyStr(n.name);
    if (t === "queue") return pyStr(or(n.kind, "queue")) + " " + pyStr(or(n.name, last(leaf.split(":"))));
    return pyStr(or(n.name, last(psplit(leaf, ":", 1))));
  };
  Index.prototype.evidence = function (nid) {
    var n = this.node(nid) || {}, ev = or(n.evidence, {});
    var f = typeof ev === "object" && !Array.isArray(ev) ? ev.file : null;
    if (!truthy(f)) return null;
    var line = or(n.start_line, ev.line);
    return truthy(line) ? f + ":" + pyStr(line) : pyStr(f);
  };
  Index.prototype.brief = function (nid) {
    var n = this.node(nid) || {};
    var out = { id: nid, type: this.kind(nid), label: this.label(nid) };
    var repo = this.repoOf(nid);
    if (truthy(repo)) out.repo = repo;
    var ev = this.evidence(nid);
    if (truthy(ev)) out.evidence = ev;
    if (n.type === "entrypoint") ["kind", "method", "path"].forEach(function (k) { if (truthy(n[k])) out[k] = n[k]; });
    if (DATA_TYPES.indexOf(n.type) >= 0 || n.type === "column") {
      out.store = this.store(nid);
      if (truthy(n.db)) out.db = n.db;
    }
    if (n.type === "queue") out.name = or(n.name, last(nid.split(":")));
    return out;
  };
  function edgeEv(e) {
    var ev = e.evidence;
    if (!ev || !truthy(ev.file)) return null;
    return truthy(ev.line) ? ev.file + ":" + pyStr(ev.line) : pyStr(ev.file);
  }

  // ── resolving the entity ──
  function QueryError(message, payload, code) { this.message = message; this.payload = payload; this.code = code; }
  function entityDict(ix, ent) {
    var stores = uniqSorted(ent.nodes.map(function (n) { return ix.store(n); }));
    return {
      kind: ent.kind, name: ent.name,
      store: stores.length === 1 ? stores[0] : stores,
      repos: uniqSorted(ent.nodes.map(function (n) { return ix.repoOf(n); }).filter(truthy)),
      nodes: ent.nodes.map(function (n) { return ix.brief(n); }),
    };
  }
  function dbKey(ix, nid) {
    var n = ix.node(nid) || {};
    if (n.type === "column") { var t = ix.tableOfColumn(nid); return t ? dbKey(ix, t) : ""; }
    return pyStr(or(n.db, "")).toLowerCase();
  }
  function group(ix, ids) {
    var byKind = new Map(), keys = [];
    ids.forEach(function (nid) {
      var tk = [pyStr(ix.node(nid).type), ix.label(nid).toLowerCase()], key = tk[0] + "\u0000" + tk[1];
      if (!byKind.has(key)) keys.push(tk);
      push(byKind, key, nid);
    });
    var groups = [];
    keys.sort(cmpTuple).forEach(function (tk) {
      var members = byKind.get(tk[0] + "\u0000" + tk[1]), dbs = new Map();
      members.forEach(function (nid) { push(dbs, dbKey(ix, nid), nid); });
      var known = Array.from(dbs.keys()).filter(function (k) { return k; });
      if (known.length <= 1) groups.push(sortStr(members));
      else {
        sortStr(known).forEach(function (k) { groups.push(sortStr(dbs.get(k))); });
        if (dbs.has("")) groups.push(sortStr(dbs.get("")));
      }
    });
    return groups;
  }
  function resolve(ix, ref) {
    ref = ref.trim();
    if (!ref) throw new QueryError("empty entity name", { schema: SCHEMA, error: "not_found", query: ref }, 1);
    var sp = split(ref), repo = sp[0], rest = sp[1], kind = null, ids;
    var all = DATA_TYPES.concat(["column"]);
    if (rest.indexOf(":") >= 0 && all.indexOf(psplit(rest, ":", 1)[0]) >= 0) { var kr = psplit(rest, ":", 1); kind = kr[0]; rest = kr[1]; }
    if (ix.has(ref) && all.indexOf(ix.kind(ref)) >= 0) ids = [ref];
    else {
      var low = rest.toLowerCase();
      if (kind === "column" || (kind === null && low.indexOf(".") >= 0 && !ix.byName.has(low))) {
        ids = (ix.colByName.get(low) || []).slice();
        if (kind === null && !ids.length) ids = (ix.byName.get(low) || []).slice();
      } else ids = (ix.byName.get(low) || []).filter(function (i) { return kind === null || ix.kind(i) === kind; });
      if (truthy(repo)) ids = ids.filter(function (i) { return ix.repoOf(i) === repo; });
    }
    if (!ids.length) {
      var hint = sortStr(Array.from(ix.byName.keys()).filter(function (k) { return k.indexOf(rest.toLowerCase()) >= 0; })).slice(0, 10);
      throw new QueryError("no table, collection or column matches " + ref, { schema: SCHEMA, error: "not_found", query: ref, similar: hint }, 1);
    }
    var groups = group(ix, ids);
    if (groups.length > 1) {
      var candidates = groups.map(function (g) {
        var first = ix.node(g[0]);
        return { kind: pyStr(first.type), name: ix.label(g[0]), store: ix.store(g[0]),
                 repos: uniqSorted(g.map(function (n) { return ix.repoOf(n); }).filter(truthy)), ids: g, qualify: qualified(ix, g) };
      });
      throw new QueryError(ref + " is ambiguous", { schema: SCHEMA, error: "ambiguous", query: ref, candidates: candidates }, 2);
    }
    var g0 = groups[0];
    return { kind: pyStr(ix.node(g0[0]).type), name: ix.label(g0[0]), nodes: g0 };
  }
  function qualified(ix, ids) {
    var first = ix.node(ids[0]), kind = pyStr(first.type), name = ix.label(ids[0]);
    if (ids.length === 1 && truthy(ix.repoOf(ids[0]))) {
      var same = kind !== "column" ? (ix.byName.get(name.toLowerCase()) || []).filter(function (i) { return ix.kind(i) === kind; })
                                   : (ix.colByName.get(name.toLowerCase()) || []);
      if (same.length === 1) return kind + ":" + name;
      return ids[0];
    }
    return kind + ":" + name;
  }

  // ── who writes / who reads ──
  function dataTargets(ix, ent) {
    var targets = new Map();
    ent.nodes.forEach(function (nid) {
      targets.set(nid, nid);
      if (ent.kind !== "column") {
        var name = pyStr(or(ix.node(nid).name, ix.label(nid))).toLowerCase();
        var repo = split(nid)[0] ? ix.repoOf(nid) : null;
        (ix.columns.get((repo === null ? "\u0000" : repo) + "\u0001" + name) || []).forEach(function (cid) {
          if (ix.tableOfColumn(cid) === nid) targets.set(cid, nid);
        });
      }
    });
    return targets;
  }
  function roots(ix, fn) {
    var cached = ix._roots.get(fn);
    if (cached) return cached;
    var parent = new Map([[fn, null]]), best = new Map([[fn, 4]]), q = [fn], qi = 0;
    var rootsL = [], tops = [], truncated = false;
    while (qi < q.length) {
      if (parent.size >= MAX_VISIT) { truncated = true; break; }
      var cur = q[qi++], t = ix.kind(cur);
      if (cur !== fn && (t === "entrypoint" || t === "queue")) { rootsL.push(cur); if (t === "queue") continue; }
      var ups = ix.up(cur);
      if (!ups.length && cur !== fn && t === "function") tops.push(cur);
      for (var i = 0; i < ups.length; i++) {
        var src = ups[i][0], rank = ups[i][1];
        if (parent.has(src)) continue;
        parent.set(src, [cur, rank]);
        best.set(src, Math.min(best.get(cur), rank));
        q.push(src);
      }
    }
    var eps = rootsL.filter(function (r) { return ix.kind(r) === "entrypoint"; });
    var epsSet = new Set(eps);
    function throughEntry(qid) {
      var c = parent.get(qid);
      while (c !== null && c !== undefined) { if (epsSet.has(c[0])) return true; c = parent.get(c[0]); }
      return false;
    }
    function feedsEntry(qid) { return ix.outOf(qid).some(function (e) { return e.type === "triggers" && epsSet.has(e.to); }); }
    var chosen = eps.concat(rootsL.filter(function (r) { return ix.kind(r) === "queue" && !throughEntry(r) && !feedsEntry(r); }));
    if (!chosen.length) chosen = tops;
    chosen = chosen.slice().sort(function (a, b) { return cmpTuple([ix.kind(a) !== "entrypoint" ? 1 : 0, ix.label(a), a], [ix.kind(b) !== "entrypoint" ? 1 : 0, ix.label(b), b]); });
    var rows = chosen.slice(0, MAX_ROOTS).map(function (r) {
      var path = [r], c = r;
      while (parent.get(c) !== null && parent.get(c) !== undefined) { c = parent.get(c)[0]; path.push(c); }
      return { root: r, path: path, confidence: BY_RANK[best.has(r) ? best.get(r) : 1] };
    });
    var result = [rows, truncated || chosen.length > MAX_ROOTS];
    ix._roots.set(fn, result);
    return result;
  }
  function accessRows(ix, ent, etype) {
    var targets = dataTargets(ix, ent), perFn = new Map();
    targets.forEach(function (owner, tid) {
      ix.incOf(tid).forEach(function (e) {
        if (e.type !== etype || e.derived) return;
        var fn = e.from, row = perFn.get(fn);
        if (!row) perFn.set(fn, row = { fn: fn, edges: [], columns: new Set(), rank: 0, evidence: null, table: owner });
        row.edges.push(e);
        if (ix.kind(tid) === "column") row.columns.add(pyStr(ix.node(tid).name));
        row.rank = Math.max(row.rank, rankOf(e.confidence));
        var ev = edgeEv(e);
        if (ev && (row.evidence === null || ev < row.evidence)) row.evidence = ev;
      });
    });
    var rows = [];
    perFn.forEach(function (row, fn) {
      var rr = ix.kind(fn) === "entrypoint" ? [[{ root: fn, path: [fn], confidence: "exact" }], false] : roots(ix, fn);
      var access = BY_RANK[row.rank || 1];
      var entries = rr[0].map(function (r) {
        var path = r.path;
        return {
          entrypoint: ix.brief(r.root),
          handler: path.length > 2 ? ix.brief(path[1]) : null,
          via: Math.max(0, path.length - 3),
          path: path.map(function (p) { return ix.brief(p); }),
          confidence: BY_RANK[Math.min(rankOf(r.confidence), rankOf(access))],
        };
      });
      rows.push({
        function: ix.brief(fn), repo: ix.repoOf(fn), entity_node: row.table,
        evidence: row.evidence || ix.evidence(fn), columns: sortStr(Array.from(row.columns)),
        confidence: access, entrypoints: entries, entrypoints_partial: rr[1],
      });
    });
    rows.sort(function (a, b) {
      return cmpTuple([a.repo || "", -rankOf(a.confidence), a.function.label, a.function.id], [b.repo || "", -rankOf(b.confidence), b.function.label, b.function.id]);
    });
    return rows;
  }
  function unresolvedData(ix, ent, etype) {
    var repos = [], seen = new Set();
    ent.nodes.forEach(function (n) { var r = ix.repoOf(n); var k = r === null ? "\u0000" : r; if (!seen.has(k)) { seen.add(k); repos.push(r); } });
    repos.sort(function (a, b) { return cmp(a || "", b || ""); });
    var family = ent.kind === "collection" ? "collection" : "table", out = [];
    repos.forEach(function (repo) {
      var nid = truthy(repo) ? repo + "/" + family + ":unknown" : family + ":unknown";
      var fns = uniqSorted(ix.incOf(nid).filter(function (e) { return e.type === etype && !e.derived; }).map(function (e) { return e.from; }));
      if (fns.length) out.push({ kind: "dynamic_" + family, access: etype, repo: repo, count: fns.length,
                                 functions: fns.slice(0, 10).map(function (f) { return ix.brief(f); }) });
    });
    return out;
  }
  function who(ix, ref, access, ent) {
    ent = ent || resolve(ix, ref);
    return {
      schema: SCHEMA, query: { command: access === "writes" ? "who-writes" : "who-reads", entity: ref },
      entity: entityDict(ix, ent), access: access, results: accessRows(ix, ent, access), unresolved: unresolvedData(ix, ent, access),
    };
  }

  // ── trace ──
  function closure(ix, start, limit) {
    limit = limit || MAX_VISIT;
    var cached = ix._closures.get(start);
    if (cached) return cached;
    var seen = new Set([start]), q = [start], qi = 0;
    while (qi < q.length && seen.size < limit) {
      var cur = q[qi++];
      ix.outOf(cur).forEach(function (e) {
        if (DOWN_EDGES.indexOf(e.type) >= 0 && !e.derived && !seen.has(e.to) && ix.kind(e.to) !== "queue") { seen.add(e.to); q.push(e.to); }
      });
    }
    ix._closures.set(start, seen);
    return seen;
  }
  function writesOf(ix, fns) {
    var out = new Set();
    fns.forEach(function (f) {
      ix.outOf(f).forEach(function (e) {
        if (e.type !== "writes" || e.derived) return;
        var to = e.to;
        if (ix.kind(to) === "column") to = ix.tableOfColumn(to) || to;
        if (last(psplit(split(to)[1], ":", 1)) === "unknown") return;
        out.add(to);
      });
    });
    return sortStr(Array.from(out));
  }
  function feedHop(ix, e, level) {
    var via = (e.via && e.via[0]) || {};
    var fns = (via.path || []).filter(function (p) { return ix.kind(p) === "function"; });
    return {
      kind: "feeds", depth: level, from: ix.brief(e.from), to: ix.brief(e.to), confidence: e.confidence === undefined ? null : e.confidence,
      columns: (e.column_map || []).map(function (c) {
        return { from: last(psplit(pyStr(c.from === undefined ? "" : c.from), ".", 1)), to: last(psplit(pyStr(c.to === undefined ? "" : c.to), ".", 1)),
                 confidence: c.confidence === undefined ? null : c.confidence };
      }),
      via: fns.map(function (f) { return ix.brief(f); }),
      evidence: edgeEv(e) || (fns.length ? ix.evidence(fns[0]) : null),
    };
  }
  function queueHop(ix, qid, producers, level, after, trigger) {
    var consumers = [], seen = new Set();
    ix.outOf(qid).forEach(function (e) {
      if (e.type !== "triggers") return;
      var c = e.to;
      if (seen.has(c)) return;
      seen.add(c);
      var fns = closure(ix, c);
      consumers.push({ consumer: ix.brief(c), repo: ix.repoOf(c), writes: writesOf(ix, fns).map(function (w) { return ix.brief(w); }),
                       confidence: e.confidence === undefined ? null : e.confidence });
    });
    var eps = new Set(consumers.filter(function (c) { return c.consumer.type === "entrypoint"; }).map(function (c) { return c.consumer.id; }));
    var handled = new Set();
    eps.forEach(function (ep) { closure(ix, ep).forEach(function (x) { handled.add(x); }); });
    consumers = consumers.filter(function (c) { return eps.has(c.consumer.id) || !handled.has(c.consumer.id); });
    var writes = new Map();
    consumers.forEach(function (c) { c.writes.forEach(function (w) { writes.set(w.id, w); }); });
    return {
      kind: "queue", depth: level, after: ix.brief(after), trigger: ix.brief(trigger), queue: ix.brief(qid),
      producers: producers.map(function (p) { return ix.brief(p); }),
      consumers: consumers.slice().sort(function (a, b) { return cmpTuple([a.repo || "", a.consumer.label], [b.repo || "", b.consumer.label]); }),
      writes: sortStr(Array.from(writes.keys())).map(function (k) { return writes.get(k); }),
    };
  }
  function trace(ix, ref, depth, direction, ent) {
    depth = depth === undefined ? 2 : depth;
    direction = direction || "both";
    ent = ent || resolve(ix, ref);
    var start = ent.nodes.map(function (n) { return ix.kind(n) === "column" ? ix.tableOfColumn(n) || n : n; });
    var hopsDown = [], hopsUp = [];
    var unresolved = { queues: [], http: [], dynamic_writes: [] };
    var seenQ = new Set(), seenFeed = new Set(), seenHttp = new Set(), notedFns = new Set();

    function noteUnresolved(fns) {
      sortStr(Array.from(fns).filter(function (f) { return !notedFns.has(f); })).forEach(function (f) {
        notedFns.add(f);
        var n = ix.node(f) || {};
        (n.unresolved_queues || []).forEach(function (uq) {
          var item = { function: ix.brief(f) };
          ["env", "reason", "kind"].forEach(function (k) { if (uq && Object.prototype.hasOwnProperty.call(uq, k)) item[k] = uq[k]; });
          unresolved.queues.push(item);
        });
      });
      ix.unresolvedHttp.forEach(function (u) {
        var from = pyStr(or(u.from, ""));
        if (fns.has(from) && !unresolved.http.some(function (x) { return x._from === pyStr(u.from); })) {
          unresolved.http.push({ _from: pyStr(u.from), function: ix.brief(pyStr(u.from)),
                                 method: u.method === undefined ? null : u.method, url: u.url === undefined ? null : u.url,
                                 reason: u.reason === undefined ? null : u.reason });
        }
      });
    }
    function writersOps(data) {
      var e0 = { kind: ix.kind(data), name: ix.label(data), nodes: [data] }, out = [];
      accessRows(ix, e0, "writes").forEach(function (row) {
        var fn = row.function.id;
        var entries = row.entrypoints.length ? row.entrypoints : [{ entrypoint: { id: fn }, path: [{ id: fn }] }];
        entries.forEach(function (e) {
          var path = new Set(e.path.map(function (p) { return p.id; }));
          path.add(fn);
          var ops = new Set(path);
          path.forEach(function (p) { ix.outOf(p).forEach(function (x) { if (x.type === "calls" && !x.derived) ops.add(x.to); }); });
          out.push([e.entrypoint.id, fn, ops]);
        });
      });
      return out;
    }

    // downstream
    var frontier = start.slice(), seenData = new Set(start);
    for (var level = 1; level <= depth; level++) {
      if (direction !== "down" && direction !== "both") break;
      var nxt = [];
      frontier.forEach(function (data) {
        ix.outOf(data).forEach(function (e) {
          if (e.type !== "feeds" || seenFeed.has(e._k)) return;
          seenFeed.add(e._k);
          hopsDown.push(feedHop(ix, e, level));
          if (!seenData.has(e.to)) { seenData.add(e.to); nxt.push(e.to); }
        });
        writersOps(data).forEach(function (op) {
          var rootId = op[0], fns = op[2];
          noteUnresolved(fns);
          sortStr(Array.from(fns)).forEach(function (f) {
            ix.outOf(f).forEach(function (e) {
              var key;
              if (e.type === "enqueues" && !e.derived) {
                key = data + "\u0000" + e.to;
                if (seenQ.has(key)) return;
                seenQ.add(key);
                var hop = queueHop(ix, e.to, [f], level, data, rootId);
                hopsDown.push(hop);
                hop.writes.forEach(function (w) { if (!seenData.has(w.id)) { seenData.add(w.id); nxt.push(w.id); } });
              } else if (e.type === "calls_http" && !e.derived) {
                key = data + "\u0000" + e.to;
                if (seenHttp.has(key)) return;
                seenHttp.add(key);
                var writes = writesOf(ix, closure(ix, e.to));
                hopsDown.push({ kind: "http", depth: level, after: ix.brief(data), trigger: ix.brief(rootId), caller: ix.brief(f), route: ix.brief(e.to),
                                confidence: e.confidence === undefined ? null : e.confidence, evidence: edgeEv(e),
                                writes: writes.map(function (w) { return ix.brief(w); }) });
                writes.forEach(function (w) { if (!seenData.has(w)) { seenData.add(w); nxt.push(w); } });
              }
            });
          });
        });
      });
      frontier = nxt;
      if (!frontier.length) break;
    }

    // upstream
    frontier = start.slice();
    var seenUp = new Set(start);
    for (level = 1; level <= depth; level++) {
      if (direction !== "up" && direction !== "both") break;
      var nxtU = [];
      frontier.forEach(function (data) {
        ix.incOf(data).forEach(function (e) {
          if (e.type !== "feeds" || seenFeed.has(e._k)) return;
          seenFeed.add(e._k);
          hopsUp.push(feedHop(ix, e, level));
          if (!seenUp.has(e.from)) { seenUp.add(e.from); nxtU.push(e.from); }
        });
        var ops = writersOps(data);
        ops.slice(0, MAX_WRITER_HOPS).forEach(function (op) {
          var rootId = op[0], writer = op[1];
          var hop = { kind: "writer", depth: level, into: ix.brief(data), trigger: ix.brief(rootId), function: ix.brief(writer) };
          var fromQueue = ix.incOf(rootId).some(function (x) { return x.type === "triggers" && ix.kind(x.from) === "queue"; });
          if (ix.kind(rootId) === "queue" || fromQueue) {
            var qs = ix.kind(rootId) === "queue" ? [rootId]
              : sortStr(ix.incOf(rootId).filter(function (x) { return x.type === "triggers" && ix.kind(x.from) === "queue"; }).map(function (x) { return x.from; }));
            hop.queues = qs.map(function (qid) {
              var producers = uniqSorted(ix.incOf(qid).filter(function (x) { return x.type === "enqueues" && !x.derived; }).map(function (x) { return x.from; }));
              return { queue: ix.brief(qid), producers: producers.map(function (p) { return ix.brief(p); }) };
            });
          }
          var callers = uniqSorted(ix.incOf(rootId).filter(function (x) { return x.type === "calls_http"; }).map(function (x) { return x.from; }));
          if (callers.length) { hop.http_callers = callers.slice(0, 20).map(function (c) { return ix.brief(c); }); hop.http_callers_total = callers.length; }
          hopsUp.push(hop);
        });
        if (ops.length > MAX_WRITER_HOPS) hopsUp.push({ kind: "more_writers", depth: level, into: ix.brief(data), count: ops.length - MAX_WRITER_HOPS });
      });
      frontier = nxtU;
      if (!frontier.length) break;
    }
    unresolvedData(ix, ent, "writes").forEach(function (u) { unresolved.dynamic_writes.push(u); });
    unresolved.http.forEach(function (item) { delete item._from; });
    return {
      schema: SCHEMA, query: { command: "trace", entity: ref, depth: depth, direction: direction },
      entity: entityDict(ix, ent), upstream: hopsUp, downstream: hopsDown, unresolved: unresolved,
    };
  }

  // ── the closing lines (query.unresolved_footer) ──
  var TXT = {
    en: { unresolved: "Could change this answer", nothing_unres: "Nothing unresolved in these repos could change this answer.",
          dyn_writes: "{n} write(s) with a {family} name built at runtime in {repo} could not be resolved",
          dyn_reads: "{n} read(s) with a {family} name built at runtime in {repo} could not be resolved",
          unres_q: "{n} publish(es) to a topic only known at runtime in the functions involved",
          unres_http: "{n} HTTP call(s) from the functions involved matched no route",
          kinds: { table: "table", view: "view", collection: "collection", column: "column" } },
    "pt-BR": { unresolved: "Pode mudar esta resposta", nothing_unres: "Nada não resolvido nestes repos pode mudar esta resposta.",
          dyn_writes: "{n} escrita(s) com nome de {family} montado em tempo de execução em {repo} não puderam ser resolvidas",
          dyn_reads: "{n} leitura(s) com nome de {family} montado em tempo de execução em {repo} não puderam ser resolvidas",
          unres_q: "{n} publicação(ões) num tópico só conhecido em tempo de execução nas funções envolvidas",
          unres_http: "{n} chamada(s) HTTP das funções envolvidas não ligaram em nenhuma rota",
          kinds: { table: "tabela", view: "view", collection: "coleção", column: "coluna" } },
  };
  function fmt(s, o) { return s.replace(/\{(\w+)\}/g, function (_, k) { return o[k]; }); }
  function footer(result, lang) {
    var T = TXT[lang] || TXT.en, lines = [];
    if (result.query.command === "trace") {
      var u = result.unresolved;
      u.dynamic_writes.forEach(function (d) {
        var fam = d.kind.replace(/^dynamic_/, "");
        lines.push(fmt(T.dyn_writes, { n: d.count, family: T.kinds[fam] || d.kind, repo: d.repo || "-" }));
      });
      if (u.queues.length) lines.push(fmt(T.unres_q, { n: u.queues.length }));
      if (u.http.length) lines.push(fmt(T.unres_http, { n: u.http.length }));
    } else {
      result.unresolved.forEach(function (d) {
        var fam = d.kind.replace(/^dynamic_/, "");
        lines.push(fmt(d.access === "writes" ? T.dyn_writes : T.dyn_reads, { n: d.count, family: T.kinds[fam] || d.kind, repo: d.repo || "-" }) +
                   ": " + d.functions.map(function (f) { return f.label; }).join(", "));
      });
    }
    if (!lines.length) lines = [T.nothing_unres];
    return [T.unresolved + ":"].concat(lines);
  }

  root.BizQuery = {
    SCHEMA: SCHEMA, Index: Index, QueryError: QueryError, resolve: resolve, group: group, qualified: qualified,
    who: who, trace: trace, roots: roots, queueHop: queueHop, closure: closure, footer: footer, rankOf: rankOf,
  };
})(typeof window !== "undefined" ? window : globalThis);

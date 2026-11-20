// BizMap export page. Draws the service overview, builds each subgraph on demand.
/* global cytoscape, BizReach */
(function () {
  "use strict";
  var t0 = performance.now();

  var I18N = {
    en: {
      search: "Search route, function, table, column…", filter: "Filter by table…",
      home: "Services", collapseAll: "Collapse all", fit: "Fit",
      services: "services", nodes: "nodes", edges: "edges",
      routes: "routes", jobs: "jobs", functions: "functions", tables: "tables",
      route: "route", job: "job", function: "function", table: "table", column: "column", queue: "queue", trigger: "DB trigger",
      service: "service", sharedData: "shared data",
      risks: "Many depend on it", risksHint: "used by 3 or more services; top 5 highlighted",
      noRisks: "No service or table is used by 3 or more services.",
      servicesList: "Services", callsOut: "calls to", callsIn: "called by", link: "link", links: "links",
      expand: "Expand", collapse: "Collapse", filterBy: "Filter by this table", clearFilter: "Clear filter",
      reads: "reads", writes: "writes", columns: "Columns", usedBy: "Used by services",
      readsWrites: "Reads / writes", reachedBy: "Reached by", reaches: "Reaches",
      more: "more", hiddenRoutes: "Routes not drawn", hiddenFuncs: "Functions not drawn",
      clickToShow: "click one to draw it",
      filterBanner: "Table {t}: {e} routes/jobs and {f} functions in {s} services read or write it.",
      noUsers: "Nothing reads or writes this table.",
      noResults: "No match.", evidence: "Defined at", why: "Why",
      tip: "Click a service to expand it, click its box again to collapse. Search centres and highlights a node.",
      generated: "Graph", unresolved: "unmatched HTTP calls",
      confidence: "confidence", legendCall: "calls", legendHttp: "HTTP", legendRead: "reads", legendWrite: "writes",
      legendVia: "through functions not drawn",
      calledFrom: "Called from other services",
    },
    "pt-BR": {
      search: "Buscar rota, função, tabela, coluna…", filter: "Filtrar por tabela…",
      home: "Serviços", collapseAll: "Recolher tudo", fit: "Ajustar",
      services: "serviços", nodes: "nós", edges: "arestas",
      routes: "rotas", jobs: "jobs", functions: "funções", tables: "tabelas",
      route: "rota", job: "job", function: "função", table: "tabela", column: "coluna", queue: "fila", trigger: "trigger do BD",
      service: "serviço", sharedData: "dados compartilhados",
      risks: "Muitos dependem dele", risksHint: "usado por 3 ou mais serviços; os 5 mais citados em destaque",
      noRisks: "Nenhum serviço ou tabela é usado por 3 ou mais serviços.",
      servicesList: "Serviços", callsOut: "chama", callsIn: "chamado por", link: "link", links: "links",
      expand: "Expandir", collapse: "Recolher", filterBy: "Filtrar por esta tabela", clearFilter: "Limpar filtro",
      reads: "lê", writes: "escreve", columns: "Colunas", usedBy: "Usada pelos serviços",
      readsWrites: "Lê / escreve", reachedBy: "Alcançada por", reaches: "Alcança",
      more: "a mais", hiddenRoutes: "Rotas não desenhadas", hiddenFuncs: "Funções não desenhadas",
      clickToShow: "clique numa para desenhá-la",
      filterBanner: "Tabela {t}: {e} rotas/jobs e {f} funções em {s} serviços leem ou escrevem nela.",
      noUsers: "Nada lê nem escreve nesta tabela.",
      noResults: "Nada encontrado.", evidence: "Definido em", why: "Por quê",
      tip: "Clique num serviço para expandi-lo e na caixa dele de novo para recolher. A busca centraliza e destaca o nó.",
      generated: "Grafo", unresolved: "chamadas HTTP sem destino",
      confidence: "confiança", legendCall: "chama", legendHttp: "HTTP", legendRead: "lê", legendWrite: "escreve",
      legendVia: "por funções não desenhadas",
      calledFrom: "Chamada por outros serviços",
    },
  };

  var D = JSON.parse(document.getElementById("bizmap-data").textContent);
  var L = I18N[D.lang] || I18N.en;
  var G = BizReach.buildIndex(D);
  var N = D.nodes, SV = D.services, NS = SV.length;
  var T_E = 0, T_F = 1, T_T = 2, T_C = 3, T_Q = 4;
  var CONF = BizReach.CONF;

  var ROUTE_CAP = 400, FUNC_CAP = 300, TABLE_CAP = 300, FILTER_CAP = 1500, ROWS = 36;
  var VIA_PER_NODE = 6, VIA_BUDGET = 1500;
  var COLW = 250, ROWH = 30, SVC_W = 190, SVC_H = 64, GAP = 70;
  var PALETTE = ["#0969da", "#8250df", "#1a7f37", "#bc4c00", "#bf3989", "#0a7b83", "#9a6700", "#cf222e",
                 "#57606a", "#6639ba", "#116329", "#953800"];
  var svcColor = function (s) { return s < 0 ? "#6e7781" : PALETTE[s % PALETTE.length]; };

  var state = { expanded: new Set(), pinned: new Set(), filter: null, focusColumn: null };
  var chosenCache = new Map(), tablesCache = new Map();
  var svcPos = [];
  var riskBy = { service: new Map(), table: new Map() };
  D.risks.forEach(function (r) { riskBy[r.kind].set(r.i, r); });

  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function fmt(s, o) { return s.replace(/\{(\w+)\}/g, function (_, k) { return o[k]; }); }
  function short(s, n) { n = n || 42; return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function typeName(i) {
    var t = N[i][0];
    if (t === T_E) return N[i][4] === "http" ? L.route : (N[i][4] || L.job);
    return [L.route, L.function, N[i][4] || L.table, L.column, L.queue, L.trigger][t] || "";
  }
  function svcName(s) { return s < 0 ? L.sharedData : SV[s].name; }
  function tableOf(i) { return N[i][0] === T_C ? G.colTable[i] : i; }

  // ── per-service choice of what to draw ────────────────────────────────────
  function svcTables(s) {
    if (tablesCache.has(s)) return tablesCache.get(s);
    var set = new Set();
    for (var i = SV[s].start; i < SV[s].end; i++) {
      ["reads", "writes"].forEach(function (k) {
        BizReach.each(G, "out", k, i, function (to) { var t = tableOf(to); if (t >= 0) set.add(t); });
      });
    }
    var list = Array.from(set).sort(function (a, b) { return a - b; });
    tablesCache.set(s, list);
    return list;
  }
  function degree(i) {
    var d = 0;
    BizReach.KINDS.forEach(function (k) {
      d += G.out[k].off[i + 1] - G.out[k].off[i] + G.inc[k].off[i + 1] - G.inc[k].off[i];
    });
    return d;
  }
  function chosen(s) {
    if (chosenCache.has(s)) return chosenCache.get(s);
    var routes = [], funcs = [], i;
    for (i = SV[s].start; i < SV[s].end; i++) {
      var t = N[i][0];
      if (t === T_E || t === T_Q) routes.push(i);
      else if (t === T_F) funcs.push(i);
    }
    var pickedF = funcs;
    if (funcs.length > FUNC_CAP) {
      var score = new Map();
      funcs.forEach(function (f) {
        var sc = 0;
        ["reads", "writes"].forEach(function (k) {
          BizReach.each(G, "out", k, f, function (_to, fl) { if (!(fl & 4)) sc = Math.max(sc, 3e6); });
        });
        if (G.inc.triggers.off[f + 1] > G.inc.triggers.off[f]) sc = Math.max(sc, 2e6);
        if (G.out.calls_http.off[f + 1] > G.out.calls_http.off[f]) sc = Math.max(sc, 2e6);
        score.set(f, sc + degree(f));
      });
      pickedF = funcs.slice().sort(function (a, b) { return score.get(b) - score.get(a) || a - b; }).slice(0, FUNC_CAP);
      pickedF.sort(function (a, b) { return a - b; });
    }
    var tables = svcTables(s);
    var res = {
      routes: routes.slice(0, ROUTE_CAP), hiddenRoutes: routes.slice(ROUTE_CAP),
      funcs: pickedF, hiddenFuncs: funcs.length > FUNC_CAP ? funcs.filter(function (f) { return pickedF.indexOf(f) < 0; }) : [],
      tables: tables.slice(0, TABLE_CAP),
    };
    chosenCache.set(s, res);
    return res;
  }

  // ── cytoscape ────────────────────────────────────────────────────────────
  var cy = cytoscape({
    container: $("cy"), minZoom: 0.02, maxZoom: 3,
    boxSelectionEnabled: false, autounselectify: false,
    style: [
      { selector: "node[label]", style: { "label": "data(label)" } },
      { selector: "node", style: {
        "font-size": 11, "text-valign": "center", "text-halign": "center",
        "min-zoomed-font-size": 6, "width": 220, "height": 22, "shape": "round-rectangle",
        "background-color": "#f6f8fa", "border-width": 1, "border-color": "#8c959f", "color": "#1f2328",
        "text-max-width": 210, "text-wrap": "ellipsis" } },
      { selector: "node.svc", style: {
        "width": SVC_W, "height": SVC_H, "font-size": 13, "font-weight": 600, "text-wrap": "wrap",
        "color": "#ffffff", "border-width": 2 } },
      { selector: "node.svc[color]", style: { "background-color": "data(color)", "border-color": "data(color)" } },
      { selector: "node.group", style: {
        "shape": "round-rectangle", "background-opacity": 0.05,
        "border-width": 2, "text-valign": "top", "text-halign": "center",
        "font-size": 15, "font-weight": 700, "padding": 18, "text-margin-y": -4 } },
      { selector: "node.group[color]", style: { "background-color": "data(color)", "border-color": "data(color)", "color": "data(color)" } },
      { selector: "node.ep", style: { "background-color": "#ddf4ff", "border-color": "#0969da" } },
      { selector: "node.queue", style: { "background-color": "#fbefff", "border-color": "#8250df" } },
      { selector: "node.fn", style: { "background-color": "#f6f8fa", "border-color": "#8c959f" } },
      { selector: "node.tbl", style: { "shape": "barrel", "background-color": "#fff8c5", "border-color": "#bf8700", "height": 26 } },
      { selector: "node.more", style: { "background-color": "#ffffff", "border-style": "dashed", "color": "#656d76" } },
      { selector: "node.risk", style: { "border-color": "#cf222e", "border-width": 4 } },
      { selector: "node.svc.risk", style: { "border-color": "#cf222e", "border-width": 5 } },
      { selector: "node:selected", style: { "overlay-color": "#0969da", "overlay-opacity": 0.15, "overlay-padding": 4 } },
      { selector: "node.flash", style: { "border-color": "#fb8500", "border-width": 6, "overlay-color": "#fb8500", "overlay-opacity": 0.25, "overlay-padding": 8 } },
      { selector: "edge", style: {
        "width": 1.2, "line-color": "#afb8c1", "target-arrow-color": "#afb8c1", "target-arrow-shape": "triangle",
        "arrow-scale": 0.8, "curve-style": "bezier", "opacity": 0.85 } },
      { selector: "edge.http", style: { "line-color": "#0969da", "target-arrow-color": "#0969da", "width": 2 } },
      { selector: "edge.reads", style: { "line-color": "#2da44e", "target-arrow-color": "#2da44e" } },
      { selector: "edge.writes", style: { "line-color": "#cf222e", "target-arrow-color": "#cf222e" } },
      { selector: "edge.via", style: { "line-style": "dashed" } },
      { selector: "edge.agg[label]", style: { "label": "data(label)" } },
      { selector: "edge.agg[w]", style: { "width": "data(w)" } },
      { selector: "edge.agg", style: {
        "font-size": 12, "text-background-color": "#ffffff", "text-background-opacity": 1,
        "text-background-padding": 2, "min-zoomed-font-size": 5 } },
      { selector: ".faded", style: { "opacity": 0.12 } },
      { selector: "edge.hl", style: { "opacity": 1, "width": 3, "z-index": 10 } },
    ],
  });

  // ── layout helpers ───────────────────────────────────────────────────────
  function initialLayout() {
    var tmp = [], i;
    var R = Math.max(300, NS * 40);
    for (i = 0; i < NS; i++) {
      var a = (2 * Math.PI * i) / Math.max(1, NS);
      tmp.push({ group: "nodes", data: { id: "s" + i }, position: { x: R * Math.cos(a), y: R * Math.sin(a) } });
    }
    D.connections.forEach(function (c, k) {
      tmp.push({ group: "edges", data: { id: "c" + k, source: "s" + c[0], target: "s" + c[1] } });
    });
    cy.add(tmp);
    if (NS > 1 && NS <= 150) {
      cy.layout({ name: "cose", animate: false, randomize: false, nodeRepulsion: function () { return 400000; },
                  idealEdgeLength: function () { return 220; }, numIter: 1000, fit: false }).run();
    } else if (NS > 150) {
      cy.layout({ name: "grid", fit: false, avoidOverlap: true, condense: true, spacingFactor: 1.3 }).run();
    }
    for (i = 0; i < NS; i++) {
      var p = cy.getElementById("s" + i).position();
      svcPos.push({ x: p.x * 1.4, y: p.y * 1.4 });
    }
    cy.elements().remove();
  }

  function gridColumns(cols) {
    // cols: arrays of element ids -> positions relative to block top-left
    var out = [], x = 0, rows = 0;
    cols.forEach(function (col) {
      if (!col.length) return;
      var sub = Math.ceil(col.length / ROWS);
      for (var k = 0; k < col.length; k++) {
        var c = Math.floor(k / ROWS), r = k % ROWS;
        out.push([col[k], x + c * COLW + COLW / 2, r * ROWH + ROWH / 2]);
      }
      rows = Math.max(rows, Math.min(col.length, ROWS));
      x += sub * COLW + 40;
    });
    return { pos: out, w: Math.max(x - 40, COLW), h: Math.max(rows, 1) * ROWH };
  }

  // Place blocks one by one at their anchor; a block that overlaps one already
  // placed slides away from it along the line between their centres, so the
  // services around an expanded block keep their direction (a ring around it).
  function separate(blocks, focusId) {
    var focus = null;
    blocks.forEach(function (b) { if (b.id === focusId) focus = b; });
    var fx = focus ? focus.x : 0, fy = focus ? focus.y : 0;
    var order = blocks.slice().sort(function (a, b) {
      if (a === focus) return -1;
      if (b === focus) return 1;
      var big = (b.kind !== "svc") - (a.kind !== "svc");
      if (big) return big;
      return (Math.hypot(a.x - fx, a.y - fy) - Math.hypot(b.x - fx, b.y - fy)) || (a.id < b.id ? -1 : 1);
    });
    var placed = [];
    order.forEach(function (b, k) {
      for (var it = 0; it < 400; it++) {
        var hit = null;
        for (var j = 0; j < placed.length; j++) {
          var P = placed[j];
          if (Math.abs(b.x - P.x) < (b.w + P.w) / 2 + GAP && Math.abs(b.y - P.y) < (b.h + P.h) / 2 + GAP) { hit = P; break; }
        }
        if (!hit) break;
        var dx = b.x - hit.x, dy = b.y - hit.y, len = Math.hypot(dx, dy);
        if (len < 1) { var a = k * 2.399963; dx = Math.cos(a); dy = Math.sin(a); len = 1; }
        dx /= len; dy /= len;
        // distance along (dx, dy) that clears `hit`
        var need = Math.min(
          Math.abs(dx) > 1e-6 ? ((b.w + hit.w) / 2 + GAP - Math.abs(b.x - hit.x)) / Math.abs(dx) : Infinity,
          Math.abs(dy) > 1e-6 ? ((b.h + hit.h) / 2 + GAP - Math.abs(b.y - hit.y)) / Math.abs(dy) : Infinity);
        b.x += dx * (need + 1); b.y += dy * (need + 1);
      }
      placed.push(b);
    });
  }

  // ── element building ─────────────────────────────────────────────────────
  function nodeClass(i) {
    var t = N[i][0];
    return t === T_E ? "ep" : t === T_Q ? "queue" : t === T_F ? "fn" : t === T_T ? "tbl" : "fn";
  }
  function nodeEl(i, parent, label) {
    var cls = nodeClass(i);
    if (N[i][0] === T_T && riskBy.table.has(i) && riskBy.table.get(i).top) cls += " risk";
    var d = { id: "n" + i, label: label || short(N[i][2]), idx: i };
    if (parent) d.parent = parent;
    return { group: "nodes", data: d, classes: cls };
  }

  function svcLabel(s) {
    var r = SV[s], risk = riskBy.service.get(s);
    return (risk && risk.top ? "⚠ " : "") + r.name + "\n" + (r.routes + r.jobs) + " " + L.routes + " · " + r.tables + " " + L.tables;
  }

  var current = { blocks: [], focusBlock: null };

  function buildNormal(focusId) {
    var visible = new Map();   // node index -> parent id
    var blocks = [], els = [];
    var assignedTable = new Map();
    var expanded = Array.from(state.expanded).sort(function (a, b) { return a - b; });
    var pinnedBy = new Map();
    state.pinned.forEach(function (i) {
      var s = N[i][0] === T_T || N[i][0] === T_C ? -1 : N[i][1];
      var key = s >= 0 && state.expanded.has(s) ? s : -1;
      if (!pinnedBy.has(key)) pinnedBy.set(key, []);
      pinnedBy.get(key).push(i);
    });

    for (var s = 0; s < NS; s++) {
      if (!state.expanded.has(s)) {
        blocks.push({ id: "s" + s, kind: "svc", s: s, x: svcPos[s].x, y: svcPos[s].y, w: SVC_W, h: SVC_H });
      }
    }
    expanded.forEach(function (s) {
      var ch = chosen(s), extra = (pinnedBy.get(s) || []).filter(function (i) {
        return ch.routes.indexOf(i) < 0 && ch.funcs.indexOf(i) < 0;
      });
      var routes = ch.routes.concat(extra.filter(function (i) { return N[i][0] !== T_F; }));
      var funcs = ch.funcs.concat(extra.filter(function (i) { return N[i][0] === T_F; }));
      var tables = ch.tables.filter(function (t) {
        if (assignedTable.has(t)) return false;
        assignedTable.set(t, "s" + s);
        return true;
      });
      var colR = routes.map(function (i) { return "n" + i; });
      if (ch.hiddenRoutes.length) colR.push("more-r-" + s);
      var colF = funcs.map(function (i) { return "n" + i; });
      if (ch.hiddenFuncs.length) colF.push("more-f-" + s);
      var g = gridColumns([colR, colF, tables.map(function (i) { return "n" + i; })]);
      routes.concat(funcs).concat(tables).forEach(function (i) { visible.set(i, "s" + s); });
      blocks.push({ id: "s" + s, kind: "group", s: s, x: svcPos[s].x, y: svcPos[s].y, w: g.w + 36, h: g.h + 50, grid: g,
                    routes: routes, funcs: funcs, tables: tables });
    });
    var dataPins = (pinnedBy.get(-1) || []).map(tableOf).filter(function (t) { return t >= 0 && !visible.has(t); });
    dataPins = Array.from(new Set(dataPins));
    if (dataPins.length) {
      var g2 = gridColumns([dataPins.map(function (i) { return "n" + i; })]);
      dataPins.forEach(function (i) { visible.set(i, "data"); });
      var cx = 0, cyy = 0;
      svcPos.forEach(function (p) { cx += p.x; cyy += p.y; });
      cx /= Math.max(1, NS); cyy /= Math.max(1, NS);
      blocks.push({ id: "data", kind: "data", s: -1, x: cx, y: cyy, w: g2.w + 36, h: g2.h + 50, grid: g2, tables: dataPins });
    }
    separate(blocks, focusId);

    // nodes
    blocks.forEach(function (b) {
      if (b.kind === "svc") {
        var risk = riskBy.service.get(b.s);
        els.push({ group: "nodes", data: { id: b.id, label: svcLabel(b.s), color: svcColor(b.s), svc: b.s },
                   classes: "svc" + (risk && risk.top ? " risk" : ""), position: { x: b.x, y: b.y } });
        return;
      }
      var color = svcColor(b.s);
      var lbl = b.kind === "data" ? L.sharedData : "▾ " + SV[b.s].name;
      els.push({ group: "nodes", data: { id: b.id, label: lbl, color: color, svc: b.s }, classes: "group" });
      var left = b.x - b.grid.w / 2, top = b.y - b.grid.h / 2 + 10;
      var byId = {};
      (b.routes || []).concat(b.funcs || []).concat(b.tables || []).forEach(function (i) { byId["n" + i] = i; });
      b.grid.pos.forEach(function (p) {
        var id = p[0], pos = { x: left + p[1], y: top + p[2] };
        if (id in byId) {
          var el = nodeEl(byId[id], b.id);
          el.position = pos;
          els.push(el);
        } else {
          var isR = id.indexOf("more-r-") === 0, ch = chosen(b.s);
          var n = isR ? ch.hiddenRoutes.length : ch.hiddenFuncs.length;
          els.push({ group: "nodes", data: { id: id, parent: b.id, label: "+" + n + " " + L.more + " " + (isR ? L.routes : L.functions), more: id },
                     classes: "more", position: pos });
        }
      });
    });

    // edges
    var agg = new Map();
    function rep(i) {
      var t = N[i][0] === T_C ? G.colTable[i] : i;
      if (t < 0) return null;
      if (visible.has(t)) return "n" + t;
      var s = N[t][1];
      if (s >= 0 && !state.expanded.has(s)) return "s" + s;
      return null;
    }
    function add(a, b, kind, via) {
      if (!b || a === b) return;
      var key = a + "|" + b + "|" + kind;
      var e = agg.get(key);
      if (e) { e.count++; if (!via) e.via = false; return; }
      agg.set(key, { a: a, b: b, kind: kind, count: 1, via: via });
    }
    var CALL_OUT = ["calls", "triggers", "enqueues", "calls_http"];
    for (var s2 = 0; s2 < NS; s2++) {
      if (state.expanded.has(s2)) continue;
      var me = "s" + s2;
      for (var i = SV[s2].start; i < SV[s2].end; i++) {
        BizReach.each(G, "out", "calls_http", i, function (to) { add(me, rep(to), "http", false); });
        if (agg.size > 200000) break;
        ["reads", "writes"].forEach(function (k) {
          BizReach.each(G, "out", k, i, function (to, fl) {
            if (fl & 4) return;
            var t = tableOf(to);
            if (t >= 0 && visible.get(t) === "data") add(me, "n" + t, k, false);
          });
        });
      }
    }
    visible.forEach(function (_p, v) {
      var t = N[v][0];
      if (t === T_T || t === T_C) return;
      // direct edges, then (breadth-first, bounded) edges through functions not drawn
      var vs = N[v][1], from = "n" + v, seen = new Set([v]), queue = [v], head = 0;
      var viaLeft = VIA_PER_NODE, budget = VIA_BUDGET;
      var emit = function (r, kind, via) {
        if (via) { if (viaLeft <= 0 || agg.has(from + "|" + r + "|" + kind)) return; viaLeft--; }
        add(from, r, kind, via);
      };
      while (head < queue.length && budget-- > 0) {
        var u = queue[head++], via = u !== v;
        if (via && viaLeft <= 0) break;
        CALL_OUT.forEach(function (k) {
          BizReach.each(G, "out", k, u, function (to) {
            var r = rep(to);
            if (r) { emit(r, k === "calls_http" ? "http" : "call", via); return; }
            if (k !== "calls_http" && N[to][1] === vs && !seen.has(to)) { seen.add(to); queue.push(to); }
          });
        });
        ["reads", "writes"].forEach(function (k) {
          BizReach.each(G, "out", k, u, function (to, fl) {
            if (fl & 4) return;
            var r = rep(to);
            if (r && r.charAt(0) === "n") emit(r, k, via);
          });
        });
      }
    });
    var k = 0;
    agg.forEach(function (e) {
      var isAgg = e.a.charAt(0) === "s" || e.b.charAt(0) === "s";
      var cls = e.kind + (e.via ? " via" : "") + (isAgg ? " agg" : "");
      var d = { id: "e" + (k++), source: e.a, target: e.b };
      if (isAgg) { d.label = String(e.count); d.w = Math.min(8, 1.5 + Math.log2(e.count + 1)); }
      els.push({ group: "edges", data: d, classes: cls });
    });
    return els;
  }

  function buildFilter() {
    var t = state.filter, R = BizReach.tableReach(G, t), els = [];
    var bySvc = function (a, b) { return (N[a][1] - N[b][1]) || a - b; };
    var eps = Array.from(R.entrypoints.keys()).sort(bySvc);
    var fns = Array.from(R.functions.keys()).sort(bySvc);
    var callers = Array.from(R.callers.keys()).filter(function (c) { return !R.functions.has(c); }).sort(bySvc);
    var hidden = Math.max(0, eps.length - FILTER_CAP) + Math.max(0, fns.length - FILTER_CAP) + Math.max(0, callers.length - FILTER_CAP);
    eps = eps.slice(0, FILTER_CAP); fns = fns.slice(0, FILTER_CAP); callers = callers.slice(0, FILTER_CAP);
    var ids = function (l) { return l.map(function (i) { return "n" + i; }); };
    var g = gridColumns([ids(callers), ids(eps), ids(fns), ["n" + t]]);
    var lab = function (i) { var s = N[i][1]; return short((s >= 0 ? SV[s].name + " · " : "") + N[i][2], 40); };
    var posOf = {};
    g.pos.forEach(function (p) { posOf[p[0]] = { x: p[1], y: p[2] }; });
    var svcs = new Set();
    callers.concat(eps, fns).forEach(function (i) {
      var el = nodeEl(i, null, lab(i));
      el.position = posOf["n" + i];
      el.classes += " svc-" + N[i][1];
      els.push(el);
      if (N[i][1] >= 0) svcs.add(N[i][1]);
    });
    var tel = nodeEl(t, null, N[t][2]);
    tel.position = posOf["n" + t];
    els.push(tel);
    var k = 0, inView = new Set(callers.concat(eps, fns));
    callers.forEach(function (c) {
      R.callers.get(c).forEach(function (ep) {
        if (inView.has(ep)) els.push({ group: "edges", data: { id: "f" + (k++), source: "n" + c, target: "n" + ep }, classes: "http" });
      });
    });
    fns.forEach(function (f) {
      var slot = R.functions.get(f);
      ["reads", "writes"].forEach(function (kind) {
        if (slot[kind]) els.push({ group: "edges", data: { id: "f" + (k++), source: "n" + f, target: "n" + t, conf: CONF[slot[kind] - 1] }, classes: kind });
      });
    });
    R.via.forEach(function (list, ep) {
      if (!inView.has(ep)) return;
      list.forEach(function (f) {
        if (inView.has(f)) els.push({ group: "edges", data: { id: "f" + (k++), source: "n" + ep, target: "n" + f }, classes: "call via" });
      });
    });
    R.entrypoints.forEach(function (_r, ep) {
      // direct entrypoint access (no function in between)
      if (inView.has(ep) && !R.via.has(ep)) els.push({ group: "edges", data: { id: "f" + (k++), source: "n" + ep, target: "n" + t }, classes: "reads" });
    });
    var banner = $("banner");
    banner.style.display = "block";
    banner.innerHTML = esc(fmt(L.filterBanner, { t: N[t][2], e: R.entrypoints.size, f: R.functions.size, s: svcs.size })) +
      (hidden ? " <span class=muted>(+" + hidden + " " + L.more + ")</span>" : "") +
      ' <button class="link" id="clear-filter">' + esc(L.clearFilter) + "</button>";
    $("clear-filter").onclick = function () { clearFilter(); };
    return { els: els, reach: R };
  }

  var lastBuild = { ms: 0, nodes: 0 };
  function render(opts) {
    opts = opts || {};
    var tb = performance.now(), els, reach = null;
    if (state.filter !== null) { var f = buildFilter(); els = f.els; reach = f.reach; }
    else { $("banner").style.display = "none"; els = buildNormal(opts.focusId); }
    cy.startBatch();
    cy.elements().remove();
    cy.add(els);
    cy.endBatch();
    if (state.filter !== null) {
      cy.nodes().forEach(function (n) {
        var i = n.data("idx");
        if (i !== undefined && N[i][1] >= 0) n.style({ "border-color": svcColor(N[i][1]), "border-width": 2 });
      });
    }
    if (opts.fit !== false) {
      var target = opts.focusId ? cy.getElementById(opts.focusId) : cy.elements();
      if (!target || target.empty()) target = cy.elements();
      cy.fit(target, 40);
      if (cy.zoom() > 1.2) cy.zoom({ level: 1.2, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
      if (cy.zoom() < 0.6 && target.isParent && target.isParent()) {
        // a big block: open at a readable zoom on its top-left (routes), not as a thumbnail
        var bb = target.boundingBox();
        cy.zoom(0.6);
        cy.pan({ x: 30 - bb.x1 * 0.6, y: 30 - bb.y1 * 0.6 });
      }
    }
    lastBuild = { ms: Math.round(performance.now() - tb), nodes: cy.nodes().length, edges: cy.edges().length };
    window.__bizmap.last = lastBuild;
    return reach;
  }

  // ── actions ──────────────────────────────────────────────────────────────
  function expand(s, fit) {
    state.expanded.add(s);
    render({ focusId: "s" + s, fit: fit });
    showService(s);
  }
  function collapse(s) {
    state.expanded.delete(s);
    state.pinned.forEach(function (i) { if (N[i][1] === s) state.pinned.delete(i); });
    render({ focusId: "s" + s });
    showService(s);
  }
  function collapseAll() {
    state.expanded.clear(); state.pinned.clear(); state.filter = null;
    render({});
    showOverview();
  }
  function setFilter(t) {
    state.filter = t;
    var reach = render({});
    showTable(t, reach);
  }
  function clearFilter() {
    state.filter = null;
    render({});
    showOverview();
  }

  function focusNode(i) {
    if (state.filter !== null) state.filter = null;
    var col = N[i][0] === T_C ? i : null;
    var target = tableOf(i);
    if (target < 0) target = i;
    var s = N[target][1];
    if (N[target][0] !== T_T && s >= 0 && !state.expanded.has(s)) state.expanded.add(s);
    render({ fit: false });
    if (cy.getElementById("n" + target).empty()) {
      state.pinned.add(target);
      render({ fit: false });
    }
    var el = cy.getElementById("n" + target);
    if (el.empty()) return;
    cy.animate({ center: { eles: el }, zoom: Math.max(cy.zoom(), 1) }, { duration: 300 });
    el.addClass("flash");
    setTimeout(function () { el.removeClass("flash"); }, 2500);
    cy.elements().unselect();
    el.select();
    highlight(el);
    showNode(target, col);
  }

  function highlight(el) {
    cy.elements().removeClass("faded hl");
    if (!el || el.isParent()) return;
    var hood = el.closedNeighborhood();
    cy.elements().not(hood).not(hood.parents()).addClass("faded");
    el.connectedEdges().addClass("hl");
  }

  // ── panel ────────────────────────────────────────────────────────────────
  function li(html) { return "<li>" + html + "</li>"; }
  function nodeLink(i, text) {
    return '<button class="link" data-node="' + i + '">' + esc(text || N[i][2]) + "</button>";
  }
  function panel(html) { $("panel").innerHTML = html; }

  function showOverview() {
    var m = D.meta, h = "<h2>" + esc(L.home) + "</h2>";
    h += '<p class="muted">' + NS + " " + L.services + " · " + m.nodes.toLocaleString() + " " + L.nodes + " · " +
         m.edges.toLocaleString() + " " + L.edges + (m.unresolved_http ? " · " + m.unresolved_http + " " + L.unresolved : "") + "</p>";
    h += '<p class="muted">' + esc(L.tip) + "</p>";
    h += "<h3>" + esc(L.risks) + "</h3><p class=muted>" + esc(L.risksHint) + "</p>";
    if (!D.risks.length) h += "<p class=muted>" + esc(L.noRisks) + "</p>";
    else {
      h += "<ul>";
      D.risks.slice(0, 30).forEach(function (r) {
        var label = (r.top ? "⚠ " : "") + r.name;
        var btn = r.kind === "service" ? '<button class="link" data-svc="' + r.i + '">' + esc(label) + "</button>"
                                       : '<button class="link" data-filter="' + r.i + '">' + esc(label) + "</button>";
        h += li('<span class="grow' + (r.top ? " risk" : "") + '">' + btn + ' <span class="kind">' +
                esc(r.kind === "service" ? L.service : L.table) + "</span></span><span class=muted>" + r.users.length + " " + L.services + "</span>");
      });
      h += "</ul>";
    }
    h += "<h3>" + esc(L.servicesList) + "</h3><ul>";
    SV.forEach(function (r, s) {
      h += li('<span class="grow"><button class="link" data-svc="' + s + '">' + esc(r.name) + "</button></span><span class=muted>" +
              (r.routes + r.jobs) + " " + L.routes + " · " + r.functions + " " + L.functions + " · " + r.tables + " " + L.tables + "</span>");
    });
    h += "</ul><h3>" + esc(L.generated) + "</h3><p class=muted>bizmap " + esc(m.graph_version || "") + " · " + esc(m.generated_at || "") + "</p>";
    panel(h);
  }

  function showService(s) {
    var r = SV[s], h = "<h2>" + esc(r.name) + ' <span class="kind">' + L.service + "</span></h2>";
    var risk = riskBy.service.get(s);
    if (risk) h += '<p class="risk">⚠ ' + esc(L.risks) + ": " + esc(risk.users.slice(0, 8).join(", ")) +
      (risk.users.length > 8 ? " +" + (risk.users.length - 8) : "") + " (" + risk.users.length + " " + L.services + ")</p>";
    h += '<p class="muted">' + r.routes + " " + L.routes + " · " + r.jobs + " " + L.jobs + " · " + r.functions + " " + L.functions + " · " + r.tables + " " + L.tables + "</p>";
    h += '<div class="actions"><button data-toggle="' + s + '">' + esc(state.expanded.has(s) ? L.collapse : L.expand) + "</button></div>";
    var outs = D.connections.filter(function (c) { return c[0] === s; });
    var ins = D.connections.filter(function (c) { return c[1] === s; });
    if (outs.length) {
      h += "<h3>" + esc(L.callsOut) + "</h3><ul>";
      outs.forEach(function (c) { h += li('<span class="grow"><button class="link" data-svc="' + c[1] + '">' + esc(SV[c[1]].name) + "</button></span><span class=muted>" + c[2] + " " + (c[2] === 1 ? L.link : L.links) + "</span>"); });
      h += "</ul>";
    }
    if (ins.length) {
      h += "<h3>" + esc(L.callsIn) + "</h3><ul>";
      ins.forEach(function (c) { h += li('<span class="grow"><button class="link" data-svc="' + c[0] + '">' + esc(SV[c[0]].name) + "</button></span><span class=muted>" + c[2] + " " + (c[2] === 1 ? L.link : L.links) + "</span>"); });
      h += "</ul>";
    }
    var tabs = svcTables(s);
    if (tabs.length) {
      h += "<h3>" + esc(L.tables) + " (" + tabs.length + ")</h3><ul>";
      tabs.slice(0, 200).forEach(function (t) { h += li('<span class="grow">' + nodeLink(t) + '</span><button class="link" data-filter="' + t + '">⛃</button>'); });
      h += "</ul>";
    }
    panel(h);
  }

  function dataRows(i) {
    var rows = new Map();
    ["reads", "writes"].forEach(function (k) {
      BizReach.each(G, "out", k, i, function (to, fl) {
        var t = tableOf(to);
        if (t < 0) return;
        var row = rows.get(t) || { r: false, w: false, cols: new Set() };
        row[k === "reads" ? "r" : "w"] = true;
        if (N[to][0] === T_C) row.cols.add(N[to][2].split(".").pop());
        rows.set(t, row);
      });
    });
    return rows;
  }

  function showNode(i, col) {
    var n = N[i], s = n[1];
    if (n[0] === T_T) return showTable(i, null, col);
    var h = "<h2>" + esc(n[2]) + ' <span class="kind">' + esc(typeName(i)) + "</span></h2>";
    h += '<p class="muted">' + esc(L.service) + ": " + (s >= 0 ? '<button class="link" data-svc="' + s + '">' + esc(SV[s].name) + "</button>" : esc(L.sharedData)) + "</p>";
    if (n[3]) h += '<p><span class="muted">' + esc(L.evidence) + ':</span> <span class="ev">' + esc(n[3]) + "</span></p>";
    if (D.why[i]) h += '<div class="why">' + esc(D.why[i]) + "</div>";
    var rows = dataRows(i);
    if (rows.size) {
      h += "<h3>" + esc(L.readsWrites) + "</h3><ul>";
      Array.from(rows.keys()).sort(function (a, b) { return a - b; }).forEach(function (t) {
        var r = rows.get(t);
        h += li('<span class="grow">' + nodeLink(t) + (r.cols.size ? ' <span class=muted>(' + esc(Array.from(r.cols).sort().join(", ")) + ")</span>" : "") + "</span>" +
                (r.r ? '<span class="rw r">' + L.reads + "</span>" : "") + (r.w ? '<span class="rw w">' + L.writes + "</span>" : ""));
      });
      h += "</ul>";
    }
    var outs = [], ins = [];
    BizReach.CALL_KINDS.forEach(function (k) {
      BizReach.each(G, "out", k, i, function (to) { outs.push(to); });
      BizReach.each(G, "inc", k, i, function (from) { ins.push(from); });
    });
    [[L.reaches, outs], [L.reachedBy, ins]].forEach(function (pair) {
      var list = Array.from(new Set(pair[1]));
      if (!list.length) return;
      h += "<h3>" + esc(pair[0]) + " (" + list.length + ")</h3><ul>";
      list.slice(0, 100).forEach(function (j) {
        var sj = N[j][1];
        h += li('<span class="grow">' + nodeLink(j) + '</span><span class="svc">' + esc(sj >= 0 ? SV[sj].name : "") + "</span>");
      });
      h += "</ul>";
    });
    panel(h);
  }

  function showTable(t, reach, col) {
    var n = N[t], h = "<h2>" + esc(n[2]) + ' <span class="kind">' + esc(typeName(t)) + "</span></h2>";
    var risk = riskBy.table.get(t);
    if (risk) h += '<p class="risk">⚠ ' + esc(L.risks) + "</p>";
    h += '<div class="actions">' + (state.filter === t
      ? '<button id="pf-clear">' + esc(L.clearFilter) + "</button>"
      : '<button data-filter="' + t + '">' + esc(L.filterBy) + "</button>") + "</div>";
    var users = new Map();
    var cols = (G.tableCols[t] || []).slice().sort(function (a, b) { return a - b; });
    var colRW = new Map();
    [t].concat(cols).forEach(function (target) {
      ["reads", "writes"].forEach(function (k) {
        BizReach.each(G, "inc", k, target, function (from, fl) {
          var s = N[from][1];
          if (s >= 0) users.set(s, true);
          if (target !== t && !(fl & 4)) {
            var c = colRW.get(target) || { r: 0, w: 0 };
            c[k === "reads" ? "r" : "w"]++;
            colRW.set(target, c);
          }
        });
      });
    });
    if (users.size) {
      h += "<h3>" + esc(L.usedBy) + " (" + users.size + ")</h3><ul>";
      Array.from(users.keys()).sort(function (a, b) { return a - b; }).forEach(function (s) {
        h += li('<span class="grow"><button class="link" data-svc="' + s + '">' + esc(SV[s].name) + "</button></span>");
      });
      h += "</ul>";
    }
    if (cols.length) {
      h += "<h3>" + esc(L.columns) + " (" + cols.length + ")</h3><ul>";
      cols.forEach(function (c) {
        var rw = colRW.get(c) || { r: 0, w: 0 };
        h += '<li class="' + (c === col ? "hlcol" : "") + '"><span class="grow">' + esc(N[c][2].split(".").pop()) + "</span>" +
             (rw.r ? '<span class="rw r">' + L.reads + " " + rw.r + "</span>" : "") + (rw.w ? '<span class="rw w">' + L.writes + " " + rw.w + "</span>" : "") + "</li>";
      });
      h += "</ul>";
    }
    if (reach) {
      if (!reach.entrypoints.size && !reach.functions.size) h += "<p class=muted>" + esc(L.noUsers) + "</p>";
      var eps = Array.from(reach.entrypoints.keys()).sort(function (a, b) { return (N[a][1] - N[b][1]) || a - b; });
      if (eps.length) {
        h += "<h3>" + esc(L.reachedBy) + " (" + eps.length + ")</h3><ul>";
        eps.slice(0, 300).forEach(function (e) {
          h += li('<span class="grow">' + nodeLink(e) + '</span><span class="svc">' + esc(svcName(N[e][1])) + '</span><span class="kind">' + CONF[reach.entrypoints.get(e) - 1] + "</span>");
        });
        h += "</ul>";
      }
      var calls = Array.from(reach.callers.keys()).filter(function (c) { return !reach.functions.has(c); })
        .sort(function (a, b) { return (N[a][1] - N[b][1]) || a - b; });
      if (calls.length) {
        h += "<h3>" + esc(L.calledFrom) + " (" + calls.length + ")</h3><ul>";
        calls.slice(0, 300).forEach(function (c) {
          var to = reach.callers.get(c).map(function (e) { return N[e][2]; }).join(", ");
          h += li('<span class="grow">' + nodeLink(c) + ' <span class=muted>→ ' + esc(short(to, 60)) + '</span></span><span class="svc">' + esc(svcName(N[c][1])) + "</span>");
        });
        h += "</ul>";
      }
      var fns = Array.from(reach.functions.keys()).sort(function (a, b) { return (N[a][1] - N[b][1]) || a - b; });
      if (fns.length) {
        h += "<h3>" + esc(L.functions) + " (" + fns.length + ")</h3><ul>";
        fns.slice(0, 300).forEach(function (f) {
          var sl = reach.functions.get(f);
          h += li('<span class="grow">' + nodeLink(f) + '</span><span class="svc">' + esc(svcName(N[f][1])) + "</span>" +
                  (sl.reads ? '<span class="rw r">' + L.reads + "</span>" : "") + (sl.writes ? '<span class="rw w">' + L.writes + "</span>" : ""));
        });
        h += "</ul>";
      }
    }
    panel(h);
    var pc = $("pf-clear");
    if (pc) pc.onclick = clearFilter;
  }

  function showHidden(s, routes) {
    var ch = chosen(s), list = routes ? ch.hiddenRoutes : ch.hiddenFuncs;
    var h = "<h2>" + esc(SV[s].name) + "</h2><h3>" + esc(routes ? L.hiddenRoutes : L.hiddenFuncs) + " (" + list.length + ")</h3><p class=muted>" + esc(L.clickToShow) + "</p><ul>";
    list.slice(0, 1000).forEach(function (i) { h += li('<span class="grow">' + nodeLink(i) + "</span>"); });
    panel(h + "</ul>");
  }

  $("panel").addEventListener("click", function (ev) {
    var b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.node !== undefined) focusNode(+b.dataset.node);
    else if (b.dataset.svc !== undefined) {
      var s = +b.dataset.svc;
      if (!state.expanded.has(s)) { state.filter = null; expand(s); } else { render({ focusId: "s" + s }); showService(s); }
    } else if (b.dataset.filter !== undefined) setFilter(+b.dataset.filter);
    else if (b.dataset.toggle !== undefined) {
      var s2 = +b.dataset.toggle;
      if (state.expanded.has(s2)) collapse(s2); else expand(s2);
    }
  });

  // ── graph events ─────────────────────────────────────────────────────────
  var lastTap = 0;
  cy.on("tap", function (ev) {
    if (ev.target === cy) { cy.elements().removeClass("faded hl"); if (state.filter === null) showOverview(); return; }
  });
  cy.on("tap", "node", function (ev) {
    if (ev.timeStamp === lastTap) return;
    lastTap = ev.timeStamp;
    var n = ev.target, d = n.data();
    if (d.more) { showHidden(+d.more.split("-")[2], d.more.indexOf("more-r-") === 0); return; }
    if (n.hasClass("svc")) {
      if (state.filter !== null) state.filter = null;
      expand(d.svc);
      return;
    }
    if (n.hasClass("group")) { if (d.svc >= 0) collapse(d.svc); return; }
    if (d.idx !== undefined) { highlight(n); showNode(d.idx); }
  });

  // ── search ───────────────────────────────────────────────────────────────
  var lower = null, selIdx = 0, hits = [];
  function searchIndex() {
    if (!lower) lower = N.map(function (r) { return r[2].toLowerCase(); });
    return lower;
  }
  function doSearch(q) {
    q = q.trim().toLowerCase();
    var box = $("results");
    if (!q) { box.style.display = "none"; hits = []; return; }
    var idx = searchIndex(), exact = [], pre = [], sub = [];
    for (var i = 0; i < idx.length; i++) {
      var t = N[i][0];
      if (t > T_Q) continue;
      var s = idx[i], p = s.indexOf(q);
      if (p < 0) continue;
      if (s === q) exact.push(i);
      else if (p === 0 || s.charAt(p - 1) === "." || s.charAt(p - 1) === "/" || s.charAt(p - 1) === " ") pre.push(i);
      else if (sub.length < 200) sub.push(i);
      if (exact.length > 60) break;
    }
    var typeOrder = function (a, b) { return [2, 1, 0, 3, 4][N[a][0]] - [2, 1, 0, 3, 4][N[b][0]] || a - b; };
    hits = exact.sort(typeOrder).concat(pre.sort(typeOrder), sub).slice(0, 60);
    selIdx = 0;
    if (!hits.length) { box.innerHTML = '<div class="r muted">' + esc(L.noResults) + "</div>"; box.style.display = "block"; return; }
    box.innerHTML = hits.map(function (i, k) {
      var s = N[i][1], isT = N[i][0] === T_T;
      return '<div class="r' + (k === 0 ? " on" : "") + '" data-i="' + i + '"><span class="kind">' + esc(typeName(i)) + '</span><span class="lbl">' + esc(N[i][2]) +
             '</span><span class="svc">' + esc(s >= 0 ? SV[s].name : "") + "</span>" + (isT ? '<button class="link" data-f="' + i + '" title="' + esc(L.filterBy) + '">⛃</button>' : "") + "</div>";
    }).join("");
    box.style.display = "block";
  }
  var timer = null;
  $("q").addEventListener("input", function (e) { clearTimeout(timer); var v = e.target.value; timer = setTimeout(function () { doSearch(v); }, 120); });
  $("q").addEventListener("keydown", function (e) {
    var box = $("results");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      selIdx = Math.max(0, Math.min(hits.length - 1, selIdx + (e.key === "ArrowDown" ? 1 : -1)));
      Array.prototype.forEach.call(box.children, function (c, k) { c.classList.toggle("on", k === selIdx); });
    } else if (e.key === "Enter") {
      clearTimeout(timer); doSearch(e.target.value);
      if (hits.length) { box.style.display = "none"; focusNode(hits[selIdx]); }
    } else if (e.key === "Escape") box.style.display = "none";
  });
  $("results").addEventListener("click", function (e) {
    var f = e.target.closest("[data-f]");
    var r = e.target.closest("[data-i]");
    $("results").style.display = "none";
    if (f) { setFilter(+f.dataset.f); return; }
    if (r) focusNode(+r.dataset.i);
  });
  document.addEventListener("click", function (e) { if (!e.target.closest(".field")) $("results").style.display = "none"; });

  // table filter box
  var tableByName = new Map();
  (function () {
    var opts = [];
    for (var i = 0; i < N.length; i++) {
      if (N[i][0] !== T_T) continue;
      var key = N[i][2] + (tableByName.has(N[i][2]) ? " (" + i + ")" : "");
      tableByName.set(key, i);
      if (opts.length < 5000) opts.push('<option value="' + esc(key) + '">');
    }
    $("tables").innerHTML = opts.join("");
  })();
  function applyTableFilter() {
    var v = $("tf").value.trim();
    if (tableByName.has(v)) setFilter(tableByName.get(v));
  }
  $("tf").addEventListener("change", applyTableFilter);
  $("tf").addEventListener("keydown", function (e) { if (e.key === "Enter") applyTableFilter(); });

  // ── boot ─────────────────────────────────────────────────────────────────
  $("q").placeholder = L.search;
  $("tf").placeholder = L.filter;
  $("btn-home").textContent = L.home;
  $("btn-collapse").textContent = L.collapseAll;
  $("btn-fit").textContent = L.fit;
  $("btn-home").onclick = function () { state.filter = null; render({}); showOverview(); };
  $("btn-collapse").onclick = collapseAll;
  $("btn-fit").onclick = function () { cy.fit(cy.elements(), 40); };
  $("title-sub").textContent = "— " + NS + " " + L.services + " · " + D.meta.nodes.toLocaleString() + " " + L.nodes;
  $("legend").innerHTML = '<i style="background:#afb8c1"></i>' + esc(L.legendCall) + '<i style="background:#0969da"></i>' + esc(L.legendHttp) +
    '<i style="background:#2da44e"></i>' + esc(L.legendRead) + '<i style="background:#cf222e"></i>' + esc(L.legendWrite) +
    '<i style="background:repeating-linear-gradient(90deg,#57606a 0 4px,transparent 4px 7px)"></i>' + esc(L.legendVia);

  window.__bizmap = {
    expand: function (s) { var t = performance.now(); expand(s); return performance.now() - t; },
    collapse: collapse, filter: function (t) { var a = performance.now(); setFilter(t); return performance.now() - a; },
    focus: focusNode, search: doSearch, state: state, data: D, cy: cy,
  };
  $("loading").remove();
  initialLayout();
  if (NS === 1) { state.expanded.add(0); render({ focusId: "s0" }); showService(0); }
  else { render({}); showOverview(); }
  window.__bizmap.readyMs = Math.round(performance.now() - t0);
  document.body.setAttribute("data-ready", "1");
})();

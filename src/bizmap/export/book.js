// BizMap export: read the system like a book. Overview first, detail on click.
// Levels: system → service → domain (routes) → route (call tree) → data access.
// Only the index is parsed when the page opens; each chunk on first use.
/* global cytoscape */
(function () {
  "use strict";
  var t0 = performance.now();

  var I18N = {
    en: {
      search: "Search route, function, table, column…", system: "System", services: "Services", frontends: "Front-ends",
      tables: "Tables", tablesLc: "tables", servicesLc: "services", domain1: "domain", map: "Map", domains: "domains", routes: "routes", route: "route", jobs: "jobs", functions: "functions",
      function: "function", table: "table", column: "column", screens: "screens", screen: "screen", unmatched: "calls with no route",
      api: "API", web: "front-end", worker: "worker", lib: "library",
      systemLead: "{s} services · {r} routes and jobs · {t} tables. Click a service to open its domains.",
      notReached: "{n} of {f} functions are not reached from any route or job (search finds them).",
      callsTo: "calls", calledBy: "called by", risks: "Many depend on it", risksHint: "used by 3 or more services",
      domain: "Domain", rule_path: "grouped by path", rule_folder: "grouped by handler folder", rule_yaml: "from bizmap.yaml",
      rule_other: "routes outside the patterns", rule_kind: "non-HTTP entrypoints",
      colRoute: "Route", colHandler: "Handler", colTables: "Reads / writes", colUsedBy: "Used by", colRoutes: "Routes",
      colFunctions: "Functions", colScreens: "Screens", colDb: "Database", colServices: "Services", colReaders: "Read by",
      colWriters: "Written by", colFile: "File", colUnmatched: "No route",
      openTree: "Click a route to open its call tree.", params: "Parameters", fromPath: "from the path", returns: "Returns",
      noSig: "Parameters and return types are not in the graph yet; path parameters are shown.",
      handler: "Handler", why: "Comment", calls: "Calls", callers: "Called by", readsWrites: "Reads / writes",
      viaCalls: "Through the functions it calls", httpOut: "Calls over HTTP", consumers: "Consumed by", noConsumers: "No scanned front-end or service calls this route.",
      reachedFrom: "Reached from", unresolved: "HTTP calls that matched no route", more: "more", seenAbove: "shown above",
      cycle: "calls back into the path", columns: "Columns", whoReads: "Functions that read or write it", routesReach: "Routes and jobs that reach it",
      usedByScreens: "Routes it uses", screensLead: "{n} files call the API · {u} calls matched no route.",
      unmatchedLead: "Calls from this front-end that matched no route, by reason.", allTables: "All tables",
      filterTables: "Filter tables…", reason_target_not_scanned: "target not scanned", reason_base_unknown: "base URL unknown",
      reason_path_dynamic: "path built at runtime", reason_client_unknown: "HTTP client unknown", reason_: "other",
      nothing: "Nothing here.", noResults: "No match.", evidence: "Defined at", service: "service", other: "other",
      shared: "shared", mapLead: "Services and the HTTP links between them (number = links). Click a service to open it.",
      generated: "Graph",
    },
    "pt-BR": {
      search: "Buscar rota, função, tabela, coluna…", system: "Sistema", services: "Serviços", frontends: "Front-ends",
      tables: "Tabelas", tablesLc: "tabelas", servicesLc: "serviços", domain1: "domínio", map: "Mapa", domains: "domínios", routes: "rotas", route: "rota", jobs: "jobs", functions: "funções",
      function: "função", table: "tabela", column: "coluna", screens: "telas", screen: "tela", unmatched: "chamadas sem rota",
      api: "API", web: "front-end", worker: "worker", lib: "biblioteca",
      systemLead: "{s} serviços · {r} rotas e jobs · {t} tabelas. Clique num serviço para abrir os domínios dele.",
      notReached: "{n} de {f} funções não são alcançadas por nenhuma rota ou job (a busca encontra).",
      callsTo: "chama", calledBy: "chamado por", risks: "Muitos dependem dele", risksHint: "usado por 3 ou mais serviços",
      domain: "Domínio", rule_path: "agrupado pelo path", rule_folder: "agrupado pela pasta do handler", rule_yaml: "do bizmap.yaml",
      rule_other: "rotas fora dos padrões", rule_kind: "entrypoints que não são HTTP",
      colRoute: "Rota", colHandler: "Handler", colTables: "Lê / escreve", colUsedBy: "Usada por", colRoutes: "Rotas",
      colFunctions: "Funções", colScreens: "Telas", colDb: "Banco", colServices: "Serviços", colReaders: "Lida por",
      colWriters: "Escrita por", colFile: "Arquivo", colUnmatched: "Sem rota",
      openTree: "Clique numa rota para abrir a árvore de chamadas.", params: "Parâmetros", fromPath: "do path", returns: "Retorno",
      noSig: "Parâmetros e tipos de retorno ainda não estão no grafo; aparecem os parâmetros do path.",
      handler: "Handler", why: "Comentário", calls: "Chama", callers: "Chamada por", readsWrites: "Lê / escreve",
      viaCalls: "Pelas funções que chama", httpOut: "Chamadas HTTP", consumers: "Consumida por", noConsumers: "Nenhum front-end ou serviço escaneado chama esta rota.",
      reachedFrom: "Alcançada a partir de", unresolved: "Chamadas HTTP sem rota", more: "a mais", seenAbove: "já aparece acima",
      cycle: "volta a chamar o caminho", columns: "Colunas", whoReads: "Funções que leem ou escrevem", routesReach: "Rotas e jobs que chegam nela",
      usedByScreens: "Rotas que usa", screensLead: "{n} arquivos chamam a API · {u} chamadas sem rota.",
      unmatchedLead: "Chamadas deste front-end que não ligaram em nenhuma rota, por motivo.", allTables: "Todas as tabelas",
      filterTables: "Filtrar tabelas…", reason_target_not_scanned: "destino não escaneado", reason_base_unknown: "URL base desconhecida",
      reason_path_dynamic: "path montado em tempo de execução", reason_client_unknown: "cliente HTTP desconhecido", reason_: "outro",
      nothing: "Nada aqui.", noResults: "Nada encontrado.", evidence: "Definido em", service: "serviço", other: "outros",
      shared: "compartilhada", mapLead: "Serviços e os links HTTP entre eles (número = links). Clique num serviço para abri-lo.",
      generated: "Grafo",
    },
  };

  var $ = function (id) { return document.getElementById(id); };
  var IX = JSON.parse($("bm-index").textContent);
  var L = I18N[IX.lang] || I18N.en;
  var SV = IX.services, DOM = IX.domains;
  var T_E = 0, T_F = 1, T_T = 2, T_C = 3;
  var R = 1, W = 2;
  var cache = {}, names = null, lowerNames = null;
  var tableRow = {};
  IX.tables.forEach(function (t) { tableRow[t.i] = t; });
  var tableByName = {};
  Object.keys(IX.tableNames).forEach(function (k) { tableByName[IX.tableNames[k].toLowerCase()] = +k; });

  function chunk(cid) {
    if (!cache[cid]) cache[cid] = JSON.parse($("bm-" + cid).textContent);
    return cache[cid];
  }
  function getNames() {
    if (!names) names = chunk("names");
    return names;
  }
  function homeOf(i) { var h = getNames().h[i]; return h >= 0 ? IX.chunks[h] : null; }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function fmt(s, o) { return s.replace(/\{(\w+)\}/g, function (_, k) { return o[k]; }); }
  function num(n) { return Number(n).toLocaleString(); }
  function dirOf(ev) { var f = (ev || "").replace(/:\d+$/, ""); var k = f.lastIndexOf("/"); return k > 0 ? f.slice(0, k) : ""; }
  function svcName(s) { return s >= 0 && SV[s] ? SV[s].name : L.shared; }

  function label(i, rec) {
    if (rec && rec[0]) return rec[0];
    if (IX.tableNames[i] !== undefined) return IX.tableNames[i];
    return getNames().l[i];
  }
  function fnRecord(i, preferChunk) {
    if (preferChunk) { var c = chunk(preferChunk); if (c.fns && c.fns[i]) return c.fns[i]; }
    var h = homeOf(i);
    if (!h) return null;
    var ch = chunk(h);
    return ch.fns ? ch.fns[i] || null : null;
  }
  function rwChip(t, rw, extra) {
    var cls = rw === (R | W) ? "rw" : rw & W ? "w" : "r";
    var tag = rw === (R | W) ? "R/W" : rw & W ? "W" : "R";
    return '<a class="chip ' + cls + '" href="#/t/' + t + '" title="' + esc(tag) + '">' + esc(IX.tableNames[t] || label(t)) +
      (extra ? " <span class=muted>" + esc(extra) + "</span>" : "") + " · " + tag + "</a>";
  }
  function tableChips(list, max) {
    max = max || 6;
    var out = list.slice(0, max).map(function (p) { return rwChip(p[0], p[1]); }).join("");
    if (list.length > max) out += '<span class="chip">+' + (list.length - max) + "</span>";
    return out;
  }
  function pathHtml(p) {
    return esc(p).replace(/(\/)(:[\w-]+|\{[^}/]+\}|&lt;[^/]+&gt;|\[[^\]/]+\])/g, '$1<span class="pp">$2</span>');
  }
  function methodHtml(m) { return '<span class="m ' + esc(m) + '">' + esc(m) + "</span>"; }
  function routeLink(e) {
    var lbl = label(e), k = lbl.indexOf(" ");
    var s = getNames().s[e];
    return '<a href="#/r/' + e + '">' + (k > 0 ? methodHtml(lbl.slice(0, k)) + pathHtml(lbl.slice(k + 1)) : esc(lbl)) + "</a>" +
      ' <span class="muted">' + esc(svcName(s)) + "</span>";
  }
  function fnLink(i) { return '<a href="#/f/' + i + '">' + esc(label(i)) + "</a>"; }
  function nodeLink(i) {
    var t = getNames().t[i];
    if (t === T_E) return routeLink(i);
    if (t === T_T) return '<a href="#/t/' + i + '">' + esc(label(i)) + "</a>";
    return fnLink(i);
  }

  // ── layout chrome ─────────────────────────────────────────────────────────
  function crumbs(parts) {
    $("crumbs").innerHTML = [['<a href="#/">' + esc(L.system) + "</a>"]].concat(parts.map(function (p) { return [p]; }))
      .map(function (p) { return p[0]; }).join('<span class="sep">›</span>');
  }
  function toc(active) {
    var h = '<h4>' + esc(L.services) + "</h4>";
    SV.forEach(function (s, i) {
      if (s.kind === "web" && !s.domains.length) return;
      h += '<a href="#/s/' + i + '" class="' + (active === "s" + i ? "on" : "") + '"><span>' + esc(s.name) + '</span><span class="n">' +
           (s.routes + s.jobs ? num(s.routes + s.jobs) : esc(L[s.kind] || s.kind)) + "</span></a>";
      if (active && (active === "s" + i || (active.charAt(0) === "d" && DOM[+active.slice(1)] && DOM[+active.slice(1)].svc === i))) {
        s.domains.forEach(function (d) {
          h += '<a class="sub ' + (active === "d" + d ? "on" : "") + '" href="#/d/' + d + '"><span>' + esc(DOM[d].name === "other" ? L.other : DOM[d].name) +
               '</span><span class="n">' + DOM[d].routes + "</span></a>";
        });
      }
    });
    var fronts = SV.map(function (s, i) { return [s, i]; }).filter(function (p) { return p[0].kind === "web"; });
    if (fronts.length) {
      h += "<h4>" + esc(L.frontends) + "</h4>";
      fronts.forEach(function (p) {
        var s = p[0], i = p[1];
        h += '<a href="#/w/' + i + '" class="' + (active === "w" + i ? "on" : "") + '"><span>' + esc(s.name) + '</span><span class="n">' + num(s.screens) + " " + esc(L.screens) + "</span></a>";
        if (s.unmatched) h += '<a class="sub warn ' + (active === "u" + i ? "on" : "") + '" href="#/u/' + i + '"><span>⚠ ' + esc(L.unmatched) + '</span><span class="n">' + num(s.unmatched) + "</span></a>";
        if (s.domains.length) h += '<a class="sub" href="#/s/' + i + '"><span>' + esc(L.routes) + '</span><span class="n">' + num(s.routes + s.jobs) + "</span></a>";
      });
    }
    h += "<h4>" + esc(L.tables) + '</h4><a href="#/t" class="' + (active === "t" ? "on" : "") + '"><span>' + esc(L.allTables) + '</span><span class="n">' + num(IX.tables.length) + "</span></a>";
    h += '<h4>' + esc(L.map) + '</h4><a href="#/map" class="' + (active === "map" ? "on" : "") + '"><span>' + esc(L.map) + "</span></a>";
    $("toc").innerHTML = h;
  }
  function read(html) { $("read").innerHTML = html; $("read").scrollTop = 0; }
  function detail(html) { $("detail").innerHTML = html || ""; }

  // ── level 0: system ──────────────────────────────────────────────────────
  function pageSystem() {
    crumbs([]); toc(null);
    var routes = 0; SV.forEach(function (s) { routes += s.routes + s.jobs; });
    var h = "<h1>" + esc(L.system) + '</h1><p class="lead">' + esc(fmt(L.systemLead, { s: SV.length, r: num(routes), t: num(IX.tables.length) })) + "</p>";
    var m = IX.meta;
    if (m.functions) h += '<p class="lead">' + esc(fmt(L.notReached, { n: num(m.functions - m.reached), f: num(m.functions) })) + "</p>";
    var groups = [["api", L.api], ["web", L.web], ["worker", L.worker], ["lib", L.lib]];
    groups.forEach(function (g) {
      var list = SV.map(function (s, i) { return [s, i]; }).filter(function (p) { return p[0].kind === g[0]; });
      if (!list.length) return;
      h += "<h2>" + esc(g[1]) + " (" + list.length + ')</h2><div class="cards">';
      list.forEach(function (p) {
        var s = p[0], i = p[1];
        var to = IX.connections.filter(function (c) { return c[0] === i; }).map(function (c) { return SV[c[1]].name; });
        var from = IX.connections.filter(function (c) { return c[1] === i; }).map(function (c) { return SV[c[0]].name; });
        var risk = IX.risks.filter(function (r) { return r.kind === "service" && r.i === i; })[0];
        var href = s.kind === "web" ? "#/w/" + i : "#/s/" + i;
        h += '<div class="card" data-href="' + href + '"><h3>' + (risk ? '<span class="risk">⚠</span>' : "") + esc(s.name) +
             ' <span class="kind">' + esc(L[s.kind] || s.kind) + '</span></h3><div class="stats">';
        if (s.routes + s.jobs) h += num(s.routes + s.jobs) + " " + esc(L.routes) + " · " + s.domains.length + " " + esc(s.domains.length === 1 ? L.domain1 : L.domains) + "<br>";
        if (s.kind === "web") h += num(s.screens) + " " + esc(L.screens) + (s.unmatched ? ' · <span class="risk">' + num(s.unmatched) + " " + esc(L.unmatched) + "</span>" : "") + "<br>";
        h += num(s.functions) + " " + esc(L.functions) + (s.tables ? " · " + num(s.tables) + " " + esc(L.tablesLc) : "") + "<br>";
        if (to.length) h += esc(L.callsTo) + ": " + esc(short(to)) + "<br>";
        if (from.length) h += esc(L.calledBy) + ": " + esc(short(from));
        h += "</div></div>";
      });
      h += "</div>";
    });
    if (IX.risks.length) {
      h += "<h2>" + esc(L.risks) + ' <span class="muted">· ' + esc(L.risksHint) + "</span></h2><ul>";
      IX.risks.slice(0, 10).forEach(function (r) {
        var link = r.kind === "service" ? '<a href="#/s/' + r.i + '">' + esc(r.name) + "</a>" : '<a href="#/t/' + r.i + '">' + esc(r.name) + "</a>";
        h += "<li>" + (r.top ? '<span class="risk">⚠</span> ' : "") + link + ' <span class="kind">' + esc(r.kind === "service" ? L.service : L.table) +
             '</span> <span class="muted">' + r.users.length + " " + esc(L.servicesLc) + ": " + esc(short(r.users)) + "</span></li>";
      });
      h += "</ul>";
    }
    h += '<p class="muted" style="margin-top:28px">' + esc(L.generated) + ": bizmap " + esc(m.graph_version || "") + " · " + esc(m.generated_at || "") + " · " + num(m.nodes) + " nodes · " + num(m.edges) + " edges</p>";
    read(h); detail("");
  }
  function short(list) { return list.length > 4 ? list.slice(0, 4).join(", ") + " +" + (list.length - 4) : list.join(", "); }

  // ── level 1: service ─────────────────────────────────────────────────────
  function pageService(s) {
    var sv = SV[s];
    crumbs(['<a href="#/s/' + s + '">' + esc(sv.name) + "</a>"]); toc("s" + s);
    var h = "<h1>" + esc(sv.name) + ' <span class="kind">' + esc(L[sv.kind] || sv.kind) + '</span></h1><p class="lead">' +
            num(sv.routes + sv.jobs) + " " + esc(L.routes) + " · " + sv.domains.length + " " + esc(sv.domains.length === 1 ? L.domain1 : L.domains) + " · " + num(sv.tables) + " " + esc(L.tablesLc) + "</p>";
    if (sv.kind === "web") h += '<p><a href="#/w/' + s + '">' + num(sv.screens) + " " + esc(L.screens) + "</a></p>";
    if (!sv.domains.length) { read(h + '<p class="empty">' + esc(L.nothing) + "</p>"); serviceDetail(s); return; }
    h += '<table class="list"><thead><tr><th>' + esc(L.domain) + '</th><th class="num">' + esc(L.colRoutes) + '</th><th class="num">' +
         esc(L.colFunctions) + "</th><th>" + esc(L.colTables) + '</th><th class="num">' + esc(L.colUsedBy) + "</th></tr></thead><tbody>";
    sv.domains.forEach(function (d) {
      var dm = DOM[d];
      h += '<tr class="row" data-href="#/d/' + d + '"><td><b>' + esc(dm.name === "other" ? L.other : dm.name) + '</b><br><span class="muted">' + esc(L["rule_" + dm.rule] || dm.rule) +
           '</span></td><td class="num">' + dm.routes + '</td><td class="num">' + num(dm.functions) + "</td><td>" + tableChips(dm.tables, 5) +
           (dm.ntables > dm.tables.length ? '<span class="chip">+' + (dm.ntables - dm.tables.length) + "</span>" : "") +
           '</td><td class="num">' + (dm.consumers ? num(dm.consumers) : "—") + "</td></tr>";
    });
    read(h + "</tbody></table>");
    serviceDetail(s);
  }
  function serviceDetail(s) {
    var sv = SV[s], h = "<h2>" + esc(sv.name) + '</h2><p class="muted">' + esc(L[sv.kind] || sv.kind) + "</p>";
    var to = IX.connections.filter(function (c) { return c[0] === s; });
    var from = IX.connections.filter(function (c) { return c[1] === s; });
    if (to.length) { h += "<h3>" + esc(L.callsTo) + "</h3><ul>" + to.map(function (c) { return '<li><a href="#/s/' + c[1] + '">' + esc(SV[c[1]].name) + '</a> <span class="muted">' + c[2] + "</span></li>"; }).join("") + "</ul>"; }
    if (from.length) { h += "<h3>" + esc(L.calledBy) + "</h3><ul>" + from.map(function (c) { return '<li><a href="#/s/' + c[0] + '">' + esc(SV[c[0]].name) + '</a> <span class="muted">' + c[2] + "</span></li>"; }).join("") + "</ul>"; }
    detail(h);
  }

  // ── level 2: domain (routes) and level 3: route tree ─────────────────────
  var openRoute = null;
  function pageDomain(d, route) {
    var dm = DOM[d], s = dm.svc, ch = chunk(dm.chunk);
    var name = dm.name === "other" ? L.other : dm.name;
    crumbs(['<a href="#/s/' + s + '">' + esc(SV[s].name) + "</a>", '<a href="#/d/' + d + '">' + esc(name) + "</a>"]);
    toc("d" + d);
    var h = "<h1>" + esc(name) + '</h1><p class="lead">' + esc(SV[s].name) + " · " + dm.routes + " " + esc(L.routes) + " · " + num(dm.functions) + " " +
            esc(L.functions) + " · " + esc(L["rule_" + dm.rule] || dm.rule) + ". " + esc(L.openTree) + "</p>";
    h += '<table class="list"><thead><tr><th>' + esc(L.colRoute) + "</th><th>" + esc(L.colHandler) + "</th><th>" + esc(L.colTables) +
         '</th><th class="num">' + esc(L.colUsedBy) + "</th></tr></thead><tbody>";
    ch.routes.forEach(function (r) {
      var hl = r.h.length ? label(r.h[0], ch.fns[r.h[0]]) : "";
      var isHttp = /^[A-Z]+$/.test(r.m) && r.p.charAt(0) === "/";
      h += '<tr class="row' + (route === r.i ? " on" : "") + '" data-route="' + r.i + '"><td>' + (isHttp ? methodHtml(r.m) + pathHtml(r.p) : '<span class="kind">' + esc(r.m) + "</span> " + esc(r.p)) +
           '</td><td class="mono">' + esc(shortFn(hl)) + "</td><td>" + tableChips(r.t, 4) + '</td><td class="num">' + (r.nc ? num(r.nc) : "—") + "</td></tr>";
      if (route === r.i) h += '<tr><td colspan="4"><div class="tree" id="tree-' + r.i + '"></div></td></tr>';
    });
    read(h + "</tbody></table>");
    if (route !== undefined && route !== null) {
      var r = ch.routes.filter(function (x) { return x.i === route; })[0];
      if (r) {
        renderTree($("tree-" + r.i), r.h, dm.chunk);
        routeDetail(r, dm);
        var row = document.querySelector('tr[data-route="' + r.i + '"]');
        if (row) row.scrollIntoView({ block: "nearest" });
      }
    } else detail('<p class="muted">' + esc(L.openTree) + "</p>");
  }
  function shortFn(s) { return s.length > 48 ? "…" + s.slice(-47) : s; }

  function renderTree(root, starts, cid) {
    var ch = chunk(cid), shown = new Set();
    function rowHtml(i, path) {
      var rec = ch.fns[i] || fnRecord(i);
      var kids = rec ? rec[3].filter(function (k) { return path.indexOf(k) < 0; }) : [];
      var cyc = rec ? rec[3].some(function (k) { return path.indexOf(k) >= 0; }) : false;
      var seen = shown.has(i);
      shown.add(i);
      var chips = "";
      if (rec) {
        rec[6].forEach(function (d) { chips += rwChip(d[0], d[1], d[2].length ? d[2].slice(0, 3).join(",") + (d[2].length > 3 ? "…" : "") : ""); });
        rec[7].forEach(function (e) { chips += '<a class="chip http" href="#/r/' + e + '">→ ' + esc(label(e)) + "</a>"; });
        if (rec[8].length) chips += '<span class="chip bad">⚠ ' + rec[8].length + " " + esc(L.unmatched) + "</span>";
      }
      return '<div class="node" data-fn="' + i + '" data-path="' + path.concat([i]).join(",") + '"><span class="tog">' + (kids.length ? (seen ? "▸" : "▾") : "·") + "</span>" +
        '<span class="mono">' + esc(label(i, rec)) + '</span><span class="dir">' + esc(rec ? dirOf(rec[1]) : "") + "</span>" +
        (seen && kids.length ? '<span class="seen">' + esc(L.seenAbove) + "</span>" : "") + (cyc ? '<span class="seen">↺ ' + esc(L.cycle) + "</span>" : "") +
        "<span>" + chips + '</span></div><div class="kids" data-for="' + i + '"></div>';
    }
    function expand(container, i, path, auto) {
      var rec = ch.fns[i] || fnRecord(i);
      if (!rec) return;
      var kids = rec[3].filter(function (k) { return path.indexOf(k) < 0; });
      container.innerHTML = kids.map(function (k) { return rowHtml(k, path.concat([i])); }).join("");
      if (auto > 0) {
        Array.prototype.forEach.call(container.children, function (el) {
          if (el.classList.contains("node")) {
            var k = +el.dataset.fn;
            if (el.querySelector(".seen")) return;
            var next = el.nextElementSibling;
            if (next && (ch.fns[k] || {})[3] && ch.fns[k][3].length) expand(next, k, path.concat([i]), auto - 1);
          }
        });
      }
    }
    root.innerHTML = starts.map(function (i) { return rowHtml(i, []); }).join("");
    Array.prototype.forEach.call(root.querySelectorAll(":scope > .node"), function (el) {
      expand(el.nextElementSibling, +el.dataset.fn, [], 1);
    });
    root.onclick = function (ev) {
      var a = ev.target.closest("a");
      if (a) return;
      var n = ev.target.closest(".node");
      if (!n) return;
      var i = +n.dataset.fn, kids = n.nextElementSibling, path = n.dataset.path.split(",").map(Number).slice(0, -1);
      if (ev.target.classList.contains("tog") || n.classList.contains("on")) {
        if (kids.children.length) { kids.innerHTML = ""; n.querySelector(".tog").textContent = "▸"; }
        else { expand(kids, i, path, 0); if (kids.children.length) n.querySelector(".tog").textContent = "▾"; }
      }
      Array.prototype.forEach.call(root.querySelectorAll(".node.on"), function (x) { x.classList.remove("on"); });
      n.classList.add("on");
      fnDetail(i, cid);
    };
  }

  function sigHtml(rec, pp) {
    var h = "<h3>" + esc(L.params) + "</h3>";
    var sig = rec && rec[9];
    if (sig && sig.params) {
      h += "<ul>" + sig.params.map(function (p) { return "<li><code>" + esc(p.name || p) + "</code>" + (p.type ? ' <span class="muted">' + esc(p.type) + "</span>" : "") + "</li>"; }).join("") + "</ul>";
    }
    if (pp && pp.length) h += "<ul>" + pp.map(function (p) { return '<li><code class="pp">' + esc(p) + '</code> <span class="muted">' + esc(L.fromPath) + "</span></li>"; }).join("") + "</ul>";
    if (sig && sig.returns) h += "<h3>" + esc(L.returns) + "</h3><p><code>" + esc(typeof sig.returns === "string" ? sig.returns : JSON.stringify(sig.returns)) + "</code></p>";
    if (!sig) h += '<p class="muted">' + esc(L.noSig) + "</p>";
    return h;
  }

  function routeDetail(r, dm) {
    var ch = chunk(dm.chunk), hrec = r.h.length ? ch.fns[r.h[0]] : null;
    var h = "<h2>" + (/^[A-Z]+$/.test(r.m) ? methodHtml(r.m) + pathHtml(r.p) : esc(r.m + " " + r.p)) + "</h2>";
    h += '<p class="muted">' + esc(SV[dm.svc].name) + " › " + esc(dm.name === "other" ? L.other : dm.name) + "</p>";
    if (r.ev) h += '<p class="ev">' + esc(r.ev) + "</p>";
    if (r.why) h += '<div class="why">' + esc(r.why) + "</div>";
    h += sigHtml(hrec, r.pp);
    if (r.h.length) h += "<h3>" + esc(L.handler) + "</h3><ul>" + r.h.map(function (f) { var rec = ch.fns[f]; return "<li>" + fnLink(f) + (rec ? ' <div class="ev">' + esc(rec[1]) + "</div>" : "") + "</li>"; }).join("") + "</ul>";
    if (hrec && hrec[2]) h += '<div class="why">' + esc(hrec[2]) + "</div>";
    if (r.t.length) h += "<h3>" + esc(L.readsWrites) + "</h3><ul>" + r.t.map(function (p) { return "<li>" + rwChip(p[0], p[1]) + ' <span class="muted">' + esc((tableRow[p[0]] || {}).db || "") + "</span></li>"; }).join("") + "</ul>";
    h += "<h3>" + esc(L.consumers) + " (" + num(r.nc) + ")</h3>";
    if (!r.nc) h += '<p class="muted">' + esc(L.noConsumers) + "</p>";
    else {
      var nm = getNames();
      h += "<ul>" + r.c.map(function (f) {
        var rec = fnRecord(f), s = nm.s[f], web = SV[s] && SV[s].kind === "web";
        var file = rec ? rec[1].replace(/:\d+$/, "") : "";
        return "<li>" + (web ? "🖥 " : "") + fnLink(f) + ' <span class="muted">' + esc(svcName(s)) + "</span>" + (file ? '<div class="ev">' + esc(file) + "</div>" : "") + "</li>";
      }).join("") + (r.nc > r.c.length ? '<li class="muted">+' + (r.nc - r.c.length) + " " + esc(L.more) + "</li>" : "") + "</ul>";
    }
    detail(h);
  }

  function viaTables(i, cid) {
    var ch = chunk(cid), seen = new Set([i]), stack = [i], out = {};
    while (stack.length) {
      var u = stack.pop(), rec = ch.fns[u] || (u === i ? fnRecord(u) : null);
      if (!rec) continue;
      if (u !== i) rec[6].forEach(function (d) { out[d[0]] = (out[d[0]] || 0) | d[1]; });
      rec[3].forEach(function (k) { if (!seen.has(k) && seen.size < 5000) { seen.add(k); stack.push(k); } });
    }
    return Object.keys(out).map(function (k) { return [+k, out[k]]; });
  }

  function fnDetail(i, cid) {
    var rec = (cid && chunk(cid).fns && chunk(cid).fns[i]) || fnRecord(i);
    if (!rec) { detail("<h2>" + esc(label(i)) + "</h2>"); return; }
    cid = cid || homeOf(i);
    var nm = getNames(), s = nm.s[i];
    var h = "<h2>" + esc(label(i, rec)) + ' <span class="kind">' + esc(L.function) + "</span></h2>";
    h += '<p class="muted">' + esc(svcName(s)) + "</p>";
    if (rec[1]) h += '<p><span class="muted">' + esc(L.evidence) + ':</span> <span class="ev">' + esc(rec[1]) + "</span></p>";
    if (rec[2]) h += '<div class="why">' + esc(rec[2]) + "</div>";
    h += sigHtml(rec, null);
    if (rec[6].length) h += "<h3>" + esc(L.readsWrites) + "</h3><ul>" + rec[6].map(function (d) { return "<li>" + rwChip(d[0], d[1]) + (d[2].length ? ' <span class="muted">' + esc(d[2].join(", ")) + "</span>" : "") + (d[3].length ? ' <span class="kind">' + esc(d[3].join(" ")) + "</span>" : "") + "</li>"; }).join("") + "</ul>";
    var via = viaTables(i, cid);
    if (via.length) h += "<h3>" + esc(L.viaCalls) + " (" + via.length + ")</h3><div>" + tableChips(via, 30) + "</div>";
    if (rec[7].length) h += "<h3>" + esc(L.httpOut) + "</h3><ul>" + rec[7].map(function (e) { return "<li>" + routeLink(e) + "</li>"; }).join("") + "</ul>";
    if (rec[8].length) h += "<h3>" + esc(L.unresolved) + "</h3><ul>" + rec[8].map(function (u) { return '<li><span class="chip bad">' + esc(u[0] || "?") + "</span> <code>" + esc(u[1]) + '</code> <span class="muted">' + esc(L["reason_" + u[2]] || u[2]) + "</span></li>"; }).join("") + "</ul>";
    if (rec[3].length) h += "<h3>" + esc(L.calls) + " (" + rec[3].length + ")</h3><ul>" + rec[3].slice(0, 60).map(function (k) { return "<li>" + nodeLink(k) + "</li>"; }).join("") + (rec[3].length > 60 ? '<li class="muted">+' + (rec[3].length - 60) + "</li>" : "") + "</ul>";
    if (rec[5]) h += "<h3>" + esc(L.callers) + " (" + rec[5] + ")</h3><ul>" + rec[4].map(function (k) { return "<li>" + nodeLink(k) + ' <span class="muted">' + esc(svcName(nm.s[k])) + "</span></li>"; }).join("") + (rec[5] > rec[4].length ? '<li class="muted">+' + (rec[5] - rec[4].length) + " " + esc(L.more) + "</li>" : "") + "</ul>";
    detail(h);
  }

  // function page: its own tree, and the routes of its domain that reach it
  function pageFunction(i) {
    var cid = homeOf(i), rec = fnRecord(i), nm = getNames(), s = nm.s[i];
    crumbs(['<a href="#/s/' + s + '">' + esc(svcName(s)) + "</a>", esc(label(i, rec))]);
    toc("s" + s);
    var h = "<h1 class=mono>" + esc(label(i, rec)) + '</h1><p class="lead">' + esc(L.function) + " · " + esc(svcName(s)) + (rec && rec[1] ? ' · <span class="ev">' + esc(rec[1]) + "</span>" : "") + "</p>";
    var reached = [];
    if (cid && cid.charAt(0) === "d") {
      var ch = chunk(cid);
      ch.routes.forEach(function (r) {
        var seen = new Set(r.h), stack = r.h.slice(), hit = r.h.indexOf(i) >= 0;
        while (stack.length && !hit) {
          var u = stack.pop(), x = ch.fns[u];
          if (!x) continue;
          for (var k = 0; k < x[3].length; k++) { var v = x[3][k]; if (v === i) { hit = true; break; } if (!seen.has(v)) { seen.add(v); stack.push(v); } }
        }
        if (hit) reached.push(r.i);
      });
    }
    if (reached.length) h += "<h2>" + esc(L.reachedFrom) + " (" + reached.length + ")</h2><ul>" + reached.map(function (e) { return "<li>" + routeLink(e) + "</li>"; }).join("") + "</ul>";
    h += '<h2>' + esc(L.calls) + '</h2><div class="tree" id="ftree"></div>';
    read(h);
    if (cid) renderTree($("ftree"), [i], cid);
    fnDetail(i, cid);
  }

  // ── tables ───────────────────────────────────────────────────────────────
  function pageTables(filter) {
    crumbs(['<a href="#/t">' + esc(L.tables) + "</a>"]); toc("t");
    var h = "<h1>" + esc(L.tables) + '</h1><input class="filter" id="tfilter" placeholder="' + esc(L.filterTables) + '" value="' + esc(filter || "") + '">';
    h += '<table class="list"><thead><tr><th>' + esc(L.table) + "</th><th>" + esc(L.colDb) + "</th><th>" + esc(L.colServices) +
         '</th><th class="num">' + esc(L.colReaders) + '</th><th class="num">' + esc(L.colWriters) + '</th></tr></thead><tbody id="tbody"></tbody></table>';
    read(h);
    function fill(q) {
      q = (q || "").toLowerCase();
      var rows = IX.tables.filter(function (t) { return !q || IX.tableNames[t.i].toLowerCase().indexOf(q) >= 0; });
      rows.sort(function (a, b) { return b.users.length - a.users.length || IX.tableNames[a.i].localeCompare(IX.tableNames[b.i]); });
      $("tbody").innerHTML = rows.slice(0, 1500).map(function (t) {
        return '<tr class="row" data-href="#/t/' + t.i + '"><td><b>' + esc(IX.tableNames[t.i]) + "</b>" + (t.kind !== "table" ? ' <span class="kind">' + esc(t.kind) + "</span>" : "") +
          "</td><td>" + esc(t.db) + "</td><td>" + esc(t.users.map(svcName).join(", ")) + '</td><td class="num">' + t.r + '</td><td class="num">' + t.w + "</td></tr>";
      }).join("");
    }
    fill(filter);
    $("tfilter").oninput = function (e) { fill(e.target.value); };
    detail("");
  }
  function pageTable(t, col) {
    var row = tableRow[t];
    if (!row) return pageTables();
    var ch = chunk(row.chunk), info = ch.tables[t];
    crumbs(['<a href="#/t">' + esc(L.tables) + "</a>", esc(IX.tableNames[t])]); toc("t");
    var h = "<h1>" + esc(IX.tableNames[t]) + ' <span class="kind">' + esc(row.kind) + '</span></h1><p class="lead">' + esc(row.db) + (row.users.length ? " · " + esc(row.users.map(svcName).join(", ")) : "") + "</p>";
    var bySvc = {};
    info.users.forEach(function (u) { var s = getNames().s[u[0]]; (bySvc[s] = bySvc[s] || []).push(u); });
    h += "<h2>" + esc(L.whoReads) + " (" + info.users.length + ")</h2>";
    Object.keys(bySvc).sort(function (a, b) { return a - b; }).forEach(function (s) {
      h += "<h3 class=muted>" + esc(svcName(+s)) + '</h3><table class="list"><tbody>' + bySvc[s].map(function (u) {
        var hit = col && u[2].indexOf(col) >= 0;
        return '<tr class="row' + (hit ? " on" : "") + '" data-href="#/f/' + u[0] + '"><td class="mono">' + esc(label(u[0])) + "</td><td>" + rwChip(t, u[1]) + '</td><td class="muted">' + esc(u[2].join(", ")) + "</td><td>" + (u[3].length ? '<span class="kind">' + esc(u[3].join(" ")) + "</span>" : "") + "</td></tr>";
      }).join("") + "</tbody></table>";
    });
    if (info.routes.length) {
      h += "<h2>" + esc(L.routesReach) + " (" + info.routes.length + ')</h2><table class="list"><tbody>' + info.routes.map(function (r) {
        return "<tr><td>" + routeLink(r[0]) + "</td><td>" + rwChip(t, r[1]) + "</td></tr>";
      }).join("") + "</tbody></table>";
    }
    read(h);
    var d = "<h2>" + esc(IX.tableNames[t]) + "</h2><h3>" + esc(L.columns) + " (" + info.cols.length + ")</h3><ul>" + info.cols.map(function (c) {
      return "<li" + (c[0] === col ? ' style="background:#fff8c5"' : "") + "><code>" + esc(c[0]) + '</code> <span class="muted">' + esc(c[1]) + "</span></li>";
    }).join("") + "</ul>";
    detail(d);
  }

  // ── front-ends: screens and unmatched calls ──────────────────────────────
  function pageScreens(s) {
    var sv = SV[s], ch = chunk(sv.screen_chunk);
    crumbs(['<a href="#/w/' + s + '">' + esc(sv.name) + "</a>", esc(L.screens)]); toc("w" + s);
    var h = "<h1>" + esc(sv.name) + ' <span class="kind">' + esc(L.web) + '</span></h1><p class="lead">' + esc(fmt(L.screensLead, { n: num(sv.screens), u: num(sv.unmatched) })) + "</p>";
    var groups = {};
    ch.screens.forEach(function (sc, k) { (groups[sc.g] = groups[sc.g] || []).push(k); });
    Object.keys(groups).sort().forEach(function (g) {
      h += "<h2 class=mono>" + esc(g) + '</h2><table class="list"><thead><tr><th>' + esc(L.colFile) + "</th><th>" + esc(L.usedByScreens) + '</th><th class="num">' + esc(L.colUnmatched) + "</th></tr></thead><tbody>";
      groups[g].forEach(function (k) {
        var sc = ch.screens[k];
        h += '<tr class="row" data-screen="' + k + '"><td class="mono">' + esc(sc.f.split("/").pop()) + "</td><td>" +
             sc.r.slice(0, 6).map(function (e) { return '<a class="chip http" href="#/r/' + e + '">' + esc(label(e)) + "</a>"; }).join("") + (sc.r.length > 6 ? '<span class="chip">+' + (sc.r.length - 6) + "</span>" : "") +
             '</td><td class="num">' + (sc.u ? '<span class="risk">' + sc.u + "</span>" : "—") + "</td></tr>";
      });
      h += "</tbody></table>";
    });
    read(h);
    detail("");
    $("read").onclick = function (ev) {
      var tr = ev.target.closest("tr[data-screen]");
      if (!tr) return;
      Array.prototype.forEach.call(document.querySelectorAll("tr.on"), function (x) { x.classList.remove("on"); });
      tr.classList.add("on");
      screenDetail(s, +tr.dataset.screen);
    };
  }
  function screenDetail(s, k) {
    var ch = chunk(SV[s].screen_chunk), sc = ch.screens[k];
    var h = "<h2>" + esc(sc.f.split("/").pop()) + ' <span class="kind">' + esc(L.screen) + '</span></h2><p class="ev">' + esc(sc.f) + "</p>";
    h += "<h3>" + esc(L.usedByScreens) + " (" + sc.r.length + ")</h3><ul>" + sc.r.map(function (e) { return "<li>" + routeLink(e) + "</li>"; }).join("") + "</ul>";
    h += "<h3>" + esc(L.functions) + " (" + sc.fns.length + ")</h3><ul>";
    sc.fns.forEach(function (f) {
      var rec = ch.fns[f];
      h += "<li>" + fnLink(f) + (rec && rec[8].length ? rec[8].map(function (u) { return '<div><span class="chip bad">⚠ ' + esc(u[0] || "?") + "</span> <code>" + esc(u[1]) + '</code> <span class="muted">' + esc(L["reason_" + u[2]] || u[2]) + "</span></div>"; }).join("") : "") + "</li>";
    });
    detail(h + "</ul>");
  }
  function pageUnmatched(s) {
    var sv = SV[s], ch = chunk(sv.screen_chunk);
    crumbs(['<a href="#/w/' + s + '">' + esc(sv.name) + "</a>", esc(L.unmatched)]); toc("u" + s);
    var byReason = {};
    ch.screens.forEach(function (sc) {
      sc.fns.forEach(function (f) { (ch.fns[f] || [])[8] && ch.fns[f][8].forEach(function (u) { (byReason[u[2]] = byReason[u[2]] || []).push([f, u, sc.f]); }); });
    });
    var h = "<h1>⚠ " + esc(L.unmatched) + '</h1><p class="lead">' + esc(L.unmatchedLead) + "</p>";
    Object.keys(byReason).sort(function (a, b) { return byReason[b].length - byReason[a].length; }).forEach(function (r) {
      h += "<h2>" + esc(L["reason_" + r] || r) + " (" + byReason[r].length + ')</h2><table class="list"><tbody>' + byReason[r].slice(0, 1000).map(function (x) {
        return '<tr class="row" data-href="#/f/' + x[0] + '"><td>' + '<span class="chip bad">' + esc(x[1][0] || "?") + "</span> <code>" + esc(x[1][1]) + '</code></td><td class="mono">' + esc(label(x[0])) + '</td><td class="ev">' + esc(x[2]) + "</td></tr>";
      }).join("") + "</tbody></table>";
    });
    read(h); detail("");
  }

  // ── map (service graph) ──────────────────────────────────────────────────
  function pageMap() {
    crumbs([esc(L.map)]); toc("map");
    read("<h1>" + esc(L.map) + '</h1><p class="lead">' + esc(L.mapLead) + '</p><div id="map"></div>');
    var els = SV.map(function (s, i) { return { data: { id: "s" + i, label: s.name + "\n" + (L[s.kind] || s.kind), kind: s.kind } }; });
    IX.connections.forEach(function (c, k) { els.push({ data: { id: "c" + k, source: "s" + c[0], target: "s" + c[1], label: String(c[2]), w: Math.min(8, 1.5 + Math.log2(c[2] + 1)) } }); });
    var cy = cytoscape({ container: $("map"), elements: els, minZoom: 0.1, maxZoom: 2.5, boxSelectionEnabled: false,
      style: [
        { selector: "node[label]", style: { "label": "data(label)" } },
        { selector: "node", style: { "shape": "round-rectangle", "width": 150, "height": 48, "background-color": "#0969da", "color": "#fff",
          "text-valign": "center", "text-halign": "center", "text-wrap": "wrap", "font-size": 12, "font-weight": 600 } },
        { selector: 'node[kind = "web"]', style: { "background-color": "#8250df" } },
        { selector: 'node[kind = "worker"]', style: { "background-color": "#1a7f37" } },
        { selector: 'node[kind = "lib"]', style: { "background-color": "#6e7781" } },
        { selector: "edge[label]", style: { "label": "data(label)" } },
        { selector: "edge[w]", style: { "width": "data(w)" } },
        { selector: "edge", style: { "curve-style": "bezier", "target-arrow-shape": "triangle", "line-color": "#0969da", "target-arrow-color": "#0969da",
          "opacity": 0.7, "font-size": 11, "text-background-color": "#fff", "text-background-opacity": 1, "text-background-padding": 2 } },
      ],
      layout: SV.length <= 150 ? { name: "cose", animate: false, randomize: false, nodeRepulsion: function () { return 400000; }, idealEdgeLength: function () { return 200; } }
                               : { name: "grid" } });
    cy.on("tap", "node", function (e) { var i = +e.target.id().slice(1); location.hash = SV[i].kind === "web" ? "#/w/" + i : "#/s/" + i; });
    detail("");
  }

  // ── search ───────────────────────────────────────────────────────────────
  var hits = [], sel = 0, timer = null;
  function doSearch(q) {
    q = q.trim().toLowerCase();
    var box = $("results");
    if (!q) { box.style.display = "none"; return; }
    var nm = getNames();
    if (!lowerNames) lowerNames = nm.l.map(function (x) { return x.toLowerCase(); });
    var exact = [], pre = [], sub = [];
    for (var i = 0; i < lowerNames.length; i++) {
      var t = nm.t[i];
      if (t > T_C) continue;
      var s = lowerNames[i], p = s.indexOf(q);
      if (p < 0) continue;
      if (s === q) exact.push(i);
      else if (p === 0 || "./ _:".indexOf(s.charAt(p - 1)) >= 0) { if (pre.length < 300) pre.push(i); }
      else if (sub.length < 200) sub.push(i);
    }
    var rank = [2, 1, 0, 3];
    var by = function (a, b) { return rank[nm.t[a]] - rank[nm.t[b]] || a - b; };
    hits = exact.sort(by).concat(pre.sort(by), sub).slice(0, 60);
    sel = 0;
    var kinds = [L.route, L.function, L.table, L.column];
    box.innerHTML = hits.length ? hits.map(function (i, k) {
      return '<div class="r' + (k === 0 ? " on" : "") + '" data-i="' + i + '"><span class="kind">' + esc(kinds[nm.t[i]]) + '</span><span class="lbl mono">' + esc(nm.l[i]) +
             '</span><span class="muted">' + esc(svcName(nm.s[i])) + "</span></div>";
    }).join("") : '<div class="r muted">' + esc(L.noResults) + "</div>";
    box.style.display = "block";
  }
  function go(i) {
    var nm = getNames(), t = nm.t[i];
    $("results").style.display = "none";
    if (t === T_E) location.hash = "#/r/" + i;
    else if (t === T_T) location.hash = "#/t/" + i;
    else if (t === T_C) {
      var parts = nm.l[i].split("."), col = parts.pop(), tb = tableByName[parts.join(".").toLowerCase()];
      location.hash = tb !== undefined ? "#/t/" + tb + "/" + encodeURIComponent(col) : "#/t";
    } else location.hash = "#/f/" + i;
  }
  $("q").placeholder = L.search;
  $("q").addEventListener("input", function (e) { clearTimeout(timer); var v = e.target.value; timer = setTimeout(function () { doSearch(v); }, 120); });
  $("q").addEventListener("keydown", function (e) {
    var box = $("results");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = Math.max(0, Math.min(hits.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
      Array.prototype.forEach.call(box.children, function (c, k) { c.classList.toggle("on", k === sel); });
    } else if (e.key === "Enter") { clearTimeout(timer); doSearch(e.target.value); if (hits.length) go(hits[sel]); }
    else if (e.key === "Escape") box.style.display = "none";
  });
  $("results").addEventListener("click", function (e) { var r = e.target.closest("[data-i]"); if (r) go(+r.dataset.i); });
  document.addEventListener("click", function (e) { if (!e.target.closest(".search")) $("results").style.display = "none"; });

  // ── router ───────────────────────────────────────────────────────────────
  $("read").addEventListener("click", function (ev) {
    if (ev.target.closest("a")) return;
    var card = ev.target.closest("[data-href]");
    if (card) { location.hash = card.dataset.href; return; }
    var tr = ev.target.closest("tr[data-route]");
    if (tr) {
      var r = +tr.dataset.route;
      location.hash = openRoute === r ? "#/d/" + IX.routeDomain[r] : "#/r/" + r;
    }
  });
  function route() {
    var h = location.hash.replace(/^#\/?/, "").split("/");
    var t = performance.now();
    $("read").onclick = null;
    openRoute = null;
    try {
      if (!h[0]) pageSystem();
      else if (h[0] === "s" && SV[+h[1]]) pageService(+h[1]);
      else if (h[0] === "d" && DOM[+h[1]]) pageDomain(+h[1]);
      else if (h[0] === "r" && IX.routeDomain[h[1]] !== undefined) { openRoute = +h[1]; pageDomain(IX.routeDomain[h[1]], +h[1]); }
      else if (h[0] === "f") pageFunction(+h[1]);
      else if (h[0] === "t" && h[1] !== undefined && h[1] !== "") pageTable(+h[1], h[2] ? decodeURIComponent(h[2]) : null);
      else if (h[0] === "t") pageTables();
      else if (h[0] === "w" && SV[+h[1]] && SV[+h[1]].screen_chunk) pageScreens(+h[1]);
      else if (h[0] === "u" && SV[+h[1]] && SV[+h[1]].screen_chunk) pageUnmatched(+h[1]);
      else if (h[0] === "map") pageMap();
      else pageSystem();
    } catch (err) {
      read('<p class="risk">' + esc(String(err && err.message || err)) + "</p>");
      if (window.console) console.error(err);
    }
    window.__bizmap.lastMs = Math.round(performance.now() - t);
  }
  window.__bizmap = { index: IX, chunk: chunk, names: getNames, search: doSearch, go: go };
  window.addEventListener("hashchange", route);
  route();
  window.__bizmap.readyMs = Math.round(performance.now() - t0);
  document.body.setAttribute("data-ready", "1");
})();

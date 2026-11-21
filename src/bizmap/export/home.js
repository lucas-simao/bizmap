  // ══ start screen: the question first ("who writes / reads X?") ══════════
  // Inlined into book.js (same scope). Answers come from BizQuery (query.js,
  // the CLI's engine ported) on the graph in the `qg` chunk, parsed on the
  // first question. Summary, cards and matrix come from IX.home (export time).
  var HT = {
    en: {
      home: "Start", servicesPage: "Services", ask: "Who writes / reads…?", askPh: "a table, collection, column (orders.status), route, topic, service or function",
      askHint: "Try", repos: "repos", services: "services", data: "tables / collections", eps: "entrypoints", flows: "flows between stores", unres: "unresolved links",
      finds: "What BizMap found", matrix: "Services × tables / collections", unexpected: "Unexpected writer", unexpectedLine: "{s} writes {t} (owner: {o})",
      ownerDdl: "has the DDL / migration", ownerMost: "most writes", multi: "Written by more than one service", multiLine: "{t} is written by {n} services",
      dynamic: "Names built at runtime", dynamicLead: "reads / writes whose table or collection name is only known at runtime",
      orphans: "Orphan functions", orphansLead: "functions with data access that no entrypoint, queue, main or screen reaches",
      mirrors: "Data mirrored between stores", never: "Written and never read · read and never written", neverLine: "{w} written, never read · {r} read, never written",
      none: "none", writes: "Writes", reads: "Reads", trace: "Trace", up: "Where it comes from", down: "Where it goes",
      via1: "via 1 function", viaN: "via {n} functions", noEntry: "no entrypoint reaches it", fullPath: "full path", shortPath: "short path",
      unexpectedBadge: "unexpected writer", ownerIs: "owner: {o}", writesVerb: "writes", readsVerb: "reads", noneW: "No function writes it in this graph.",
      noneR: "No function reads it in this graph.", hiddenRows: "{n} more in tests and helpers (Show details → tests and helpers).",
      filteredBy: "Only {s}", clear: "show all", sameIn: "Same {k} in {n} repos: {r}.", openMap: "Open on the map", tablePage: "Table page",
      ambiguous: "“{q}” names {n} different things. Pick one:", notFound: "No table, collection or column matches “{q}”.", similar: "Similar names:",
      details: "Show details", detI: "intermediate functions", detC: "columns", detT: "tests and helpers", conf_exact: "exact", conf_inferred: "inferred",
      conf_heuristic: "heuristic", conf_unknown: "unknown", writtenBy: "written by", triggeredBy: "triggered by", publishedBy: "published by",
      calledBy: "called over HTTP by", feeds: "feeds", queue: "topic", consumedBy: "consumed by", thenWrites: "then writes", http: "HTTP",
      nothingUp: "Nothing upstream found.", nothingDown: "Nothing downstream found.", hops: "{n} hops away", tableLevel: "table level",
      moreWriters: "+{n} more writers of {t}", mapTitle: "Map", mapPick: "Pick what to center the map on: the map never draws the whole system.",
      hop1: "1 hop", hop2: "2 hops", mapCut: "Showing {n} of {t} nodes.", filterText: "Filter by name…", anyRepo: "Any repo", anyStore: "Any store",
      sortAcc: "Most accessed", sortName: "Name", sortSvc: "Most services", showMoreCols: "Show {n} more ({t} in all)", colsShown: "{n} of {t}",
      cellHelp: "R = reads · W = writes · dashed = only heuristic links · ⚡ = also a read / write with a runtime name. Click a cell for that service's answer.",
      kindTable: "table", kindView: "view", kindCollection: "collection", kindColumn: "column", kindField: "field", kindRoute: "route", kindTopic: "topic",
      kindService: "service", kindFunction: "function", kindJob: "job", noMatch: "No match.", allOf: "All", publishes: "Publishes", consumers: "Consumers",
      topicLead: "Who publishes {q}, who consumes it and what they write.", noConsumers: "No scanned consumer.", findLead: "{n} items", epsTitle: "Entrypoints",
      unresTitle: "Unresolved links", unresLead: "What the graph could not resolve: these can change answers.", file: "file", detail: "detail",
      inService: "service", noEntity: "Nothing to center on.", empty: "No tables or collections in this graph.", ask2: "Ask another",
      allTables: "All tables", R: "R", W: "W", RW: "RW", pubEdge: "publishes", subEdge: "triggers",
    },
    "pt-BR": {
      home: "Início", servicesPage: "Serviços", ask: "Quem escreve / lê…?", askPh: "uma tabela, coleção, coluna (orders.status), rota, tópico, serviço ou função",
      askHint: "Experimente", repos: "repos", services: "serviços", data: "tabelas / coleções", eps: "pontos de entrada", flows: "fluxos entre bancos", unres: "não resolvidas",
      finds: "O que o BizMap achou", matrix: "Serviços × tabelas / coleções", unexpected: "Escritor inesperado", unexpectedLine: "{s} escreve {t} (dono: {o})",
      ownerDdl: "tem a DDL / migration", ownerMost: "maioria das escritas", multi: "Escritas por mais de um serviço", multiLine: "{t} é escrita por {n} serviços",
      dynamic: "Nomes montados em tempo de execução", dynamicLead: "leituras / escritas com nome de tabela ou coleção só conhecido em tempo de execução",
      orphans: "Funções órfãs", orphansLead: "funções com acesso a dados que nenhum ponto de entrada, fila, main ou tela alcança",
      mirrors: "Dados espelhados entre bancos", never: "Escrita e nunca lida · lida e nunca escrita", neverLine: "{w} escritas e nunca lidas · {r} lidas e nunca escritas",
      none: "nenhum", writes: "Escreve", reads: "Lê", trace: "Rastro", up: "De onde vem", down: "Para onde vai",
      via1: "via 1 função", viaN: "via {n} funções", noEntry: "nenhum ponto de entrada chega nela", fullPath: "caminho completo", shortPath: "caminho curto",
      unexpectedBadge: "escritor inesperado", ownerIs: "dono: {o}", writesVerb: "escreve", readsVerb: "lê", noneW: "Nenhuma função escreve nela neste grafo.",
      noneR: "Nenhuma função lê dela neste grafo.", hiddenRows: "Mais {n} em testes e helpers (Mostrar detalhes → testes e helpers).",
      filteredBy: "Só {s}", clear: "mostrar todos", sameIn: "Mesma {k} em {n} repos: {r}.", openMap: "Abrir no mapa", tablePage: "Página da tabela",
      ambiguous: "“{q}” nomeia {n} coisas diferentes. Escolha uma:", notFound: "Nenhuma tabela, coleção ou coluna corresponde a “{q}”.", similar: "Nomes parecidos:",
      details: "Mostrar detalhes", detI: "funções intermediárias", detC: "colunas", detT: "testes e helpers", conf_exact: "exata", conf_inferred: "inferida",
      conf_heuristic: "heurística", conf_unknown: "desconhecida", writtenBy: "escrita por", triggeredBy: "disparada por", publishedBy: "publicado por",
      calledBy: "chamada via HTTP por", feeds: "alimenta", queue: "tópico", consumedBy: "consumido por", thenWrites: "depois escreve", http: "HTTP",
      nothingUp: "Nada encontrado de onde o dado vem.", nothingDown: "Nada encontrado para onde o dado vai.", hops: "a {n} passos", tableLevel: "nível de tabela",
      moreWriters: "+{n} escritores de {t}", mapTitle: "Mapa", mapPick: "Escolha onde centrar o mapa: ele nunca desenha o sistema inteiro.",
      hop1: "1 salto", hop2: "2 saltos", mapCut: "Mostrando {n} de {t} nós.", filterText: "Filtrar por nome…", anyRepo: "Qualquer repo", anyStore: "Qualquer banco",
      sortAcc: "Mais acessadas", sortName: "Nome", sortSvc: "Mais serviços", showMoreCols: "Mostrar mais {n} ({t} no total)", colsShown: "{n} de {t}",
      cellHelp: "L = lê · E = escreve · tracejado = só ligação heurística · ⚡ = também lê / escreve com nome montado em tempo de execução. Clique numa célula para a resposta daquele serviço.",
      kindTable: "tabela", kindView: "view", kindCollection: "coleção", kindColumn: "coluna", kindField: "campo", kindRoute: "rota", kindTopic: "tópico",
      kindService: "serviço", kindFunction: "função", kindJob: "job", noMatch: "Nada encontrado.", allOf: "Todos", publishes: "Publica", consumers: "Consome",
      topicLead: "Quem publica {q}, quem consome e o que escreve.", noConsumers: "Nenhum consumidor escaneado.", findLead: "{n} itens", epsTitle: "Pontos de entrada",
      unresTitle: "Ligações não resolvidas", unresLead: "O que o grafo não conseguiu resolver: isto pode mudar as respostas.", file: "arquivo", detail: "detalhe",
      inService: "serviço", noEntity: "Nada para centrar.", empty: "Nenhuma tabela ou coleção neste grafo.", ask2: "Perguntar outra",
      allTables: "Todas as tabelas", R: "L", W: "E", RW: "LE", pubEdge: "publica", subEdge: "dispara",
    },
  };
  var H = HT[IX.lang] || HT.en;
  var HOME = IX.home || null;  // graphs exported by older versions of the page have none
  var DET = { i: false, c: false, t: false }, PRM = {};
  var QIX = null, COLS = null;

  // ── URL state: `?d=ict` (Show details) travels with every link ──
  function detStr() { return (DET.i ? "i" : "") + (DET.c ? "c" : "") + (DET.t ? "t" : ""); }
  function parseParams(s) {
    var o = {};
    (s || "").split("&").forEach(function (kv) { if (!kv) return; var k = kv.indexOf("="); o[decodeURIComponent(k < 0 ? kv : kv.slice(0, k))] = k < 0 ? "" : decodeURIComponent(kv.slice(k + 1)); });
    return o;
  }
  function hrefOf(path, params) {
    var p = {}, k;
    params = params || {};
    for (k in params) if (params[k] !== undefined && params[k] !== null && params[k] !== "") p[k] = params[k];
    if (detStr()) p.d = detStr();
    var q = Object.keys(p).map(function (x) { return encodeURIComponent(x) + "=" + encodeURIComponent(p[x]); }).join("&");
    return "#/" + path + (q ? "?" + q : "");
  }
  function qPath(ref, tab) { return "q/" + encodeURIComponent(ref) + "/" + (tab || "w"); }
  function setDetails(s) {
    s = s || "";
    DET.i = s.indexOf("i") >= 0; DET.c = s.indexOf("c") >= 0; DET.t = s.indexOf("t") >= 0;
    ["i", "c", "t"].forEach(function (k) { var el = $("det-" + k); if (el) el.checked = DET[k]; });
    var n = (DET.i ? 1 : 0) + (DET.c ? 1 : 0) + (DET.t ? 1 : 0), sum = $("det-n");
    if (sum) sum.textContent = n ? " (" + n + ")" : "";
  }
  function initDetails() {
    var box = $("det");
    if (!box) return;
    $("det-label").textContent = H.details;
    ["i", "c", "t"].forEach(function (k) {
      $("det-" + k + "-l").textContent = H["det" + k.toUpperCase()];
      $("det-" + k).onchange = function () {
        DET[k] = this.checked;
        var hp = location.hash.replace(/^#\/?/, ""), qp = hp.indexOf("?");
        var path = qp >= 0 ? hp.slice(0, qp) : hp, params = parseParams(qp >= 0 ? hp.slice(qp + 1) : "");
        delete params.d;
        history.replaceState(null, "", hrefOf(path, params));
        route();
      };
    });
  }

  // ── engine and lookups ──
  function engine() {
    if (!QIX) {
      var g = chunk("qg");
      QIX = new BizQuery.Index(g);
      QIX.hidSet = new Set(g.hid || []);
    }
    return QIX;
  }
  function meta(id) {
    var ix = engine(), k = ix.pos.get(id);
    var bi = k === undefined ? -1 : ix.g.bi[k];
    return { bi: bi, svc: bi >= 0 ? getNames().s[bi] : -1, hid: k !== undefined && ix.hidSet.has(k) };
  }
  function columns() {
    if (!COLS) COLS = IX.chunks.indexOf("qc") >= 0 ? chunk("qc").cols : [];
    return COLS;
  }
  // ents: [ref, kind, name, store, repos, nw, nr, cells, owner, ownerWhy, nwHidden, nrHidden]
  function ents() { return HOME ? HOME.ents : []; }
  var ENT_BY_REF = {};
  ents().forEach(function (e, k) { ENT_BY_REF[e[0]] = k; });
  function kindWordOf(kind, store) {
    if (kind === "column") return store === "Firestore" ? H.kindField : H.kindColumn;
    return { table: H.kindTable, view: H.kindView, collection: H.kindCollection }[kind] || kind;
  }
  function kindStoreHtml(kind, store, repos) {
    return '<span class="kind">' + esc(kindWordOf(kind, store) + " · " + store) + "</span>" + (repos && repos.length ? ' <span class="muted">' + esc(repos.join(", ")) + "</span>" : "");
  }
  function confChip(c) {
    var k = c || "unknown";
    return '<span class="conf conf-' + esc(k) + '" title="' + esc(H["conf_" + k] || k) + '">' + esc(H["conf_" + k] || k) + "</span>";
  }
  function sv(s) { return s >= 0 && SV[s] ? SV[s].name : "—"; }

  // ── the ask box (home, answer page, map page): suggestions with their kind ──
  function suggest(q) {
    q = q.trim().toLowerCase();
    if (!q) return [];
    var out = [];
    function score(name) { var s = name.toLowerCase(); if (s === q) return 0; var p = s.indexOf(q); if (p === 0) return 1; if (p > 0 && "./ _:-".indexOf(s.charAt(p - 1)) >= 0) return 2; return p > 0 ? 3 : -1; }
    function add(rank, item) { if (rank >= 0) { item.rank = rank; out.push(item); } }
    ents().forEach(function (e) { add(score(e[2]), { t: "e", ref: e[0], kind: e[1], label: e[2], store: e[3], repos: e[4], w: e[5] + e[6], o: 0 }); });
    columns().forEach(function (c) { add(score(c[1]), { t: "e", ref: c[0], kind: "column", label: c[1], store: c[2], repos: c[3], w: c[5] + c[6], o: 1 }); });
    (HOME ? HOME.topics : []).forEach(function (tp) { var name = tp[1].replace(/^\S+\s/, ""); add(Math.min(score(name), score(tp[1]) < 0 ? 9 : score(tp[1])), { t: "q", ref: tp[0], label: name, o: 3 }); });
    SV.forEach(function (s, i) { add(score(s.name), { t: "s", ref: i, label: s.name, o: 4 }); });
    var nm = getNames(), n = 0;
    for (var i = 0; i < nm.l.length && n < 400; i++) {
      var ty = nm.t[i];
      if (ty !== T_E && ty !== T_F) continue;
      var r = score(nm.l[i]);
      if (r < 0 || (r === 3 && q.length < 3)) continue;
      n++;
      add(r, { t: ty === T_E ? "r" : "f", ref: i, label: nm.l[i], s: nm.s[i], o: ty === T_E ? 2 : 5 });
    }
    out.sort(function (a, b) { return a.rank - b.rank || a.o - b.o || (b.w || 0) - (a.w || 0) || (a.label < b.label ? -1 : a.label > b.label ? 1 : 0); });
    return out.slice(0, 12);
  }
  function suggestionHtml(s, on) {
    var kind, extra = "";
    if (s.t === "e") { kind = kindWordOf(s.kind, s.store) + " · " + s.store; extra = s.repos.join(", "); }
    else if (s.t === "r") { kind = /^[A-Z]+ \//.test(s.label) ? H.kindRoute : H.kindJob; extra = sv(s.s); }
    else if (s.t === "q") kind = H.kindTopic;
    else if (s.t === "s") kind = H.kindService;
    else { kind = H.kindFunction; extra = sv(s.s); }
    return '<div class="sg' + (on ? " on" : "") + '"><span class="sg-l mono">' + esc(s.label) + '</span><span class="kind">' + esc(kind) + "</span>" +
      (extra ? '<span class="muted">' + esc(extra) + "</span>" : "") + "</div>";
  }
  function pickTarget(s, mode) {
    if (mode === "map") {
      if (s.t === "e") return hrefOf("map/e/" + encodeURIComponent(s.ref));
      if (s.t === "s") return hrefOf("map/s/" + s.ref);
      if (s.t === "q") return hrefOf("map/q/" + encodeURIComponent(s.ref));
    }
    if (s.t === "e") return hrefOf(qPath(s.ref, "w"));
    if (s.t === "r") return hrefOf("r/" + s.ref);
    if (s.t === "q") return hrefOf("topic/" + encodeURIComponent(s.ref));
    if (s.t === "s") return hrefOf("s/" + s.ref);
    return hrefOf("f/" + s.ref);
  }
  // Enter without moving the selection: a name shared by several things is
  // never picked silently — the page lists the candidates
  function typedTarget(text, list, mode) {
    var low = text.trim().toLowerCase();
    if (!low) return null;
    var same = list.filter(function (s) { return s.t === "e" && (s.label.toLowerCase() === low || String(s.ref).toLowerCase() === low); });
    if (same.length > 1 && !same.some(function (s) { return String(s.ref).toLowerCase() === low; })) return hrefOf("q", { n: text.trim() });
    if (same.length) return pickTarget(same[0], mode);
    if (list.length) return pickTarget(list[0], mode);
    return mode === "map" ? null : hrefOf("q", { n: text.trim() });
  }
  function askBox(id, mode, value) {
    return '<div class="ask-box"><input id="' + id + '" class="ask-in" type="search" autocomplete="off" spellcheck="false" placeholder="' + esc(H.askPh) + '" value="' + esc(value || "") +
      '"><div class="ask-list" id="' + id + '-list" role="listbox"></div></div>';
  }
  function wireAsk(id, mode) {
    var input = $(id), box = $(id + "-list"), list = [], sel = 0, moved = false, tm = null;
    if (!input) return;
    function show() {
      list = suggest(input.value);
      sel = 0; moved = false;
      box.innerHTML = list.length ? list.map(function (s, k) { return suggestionHtml(s, k === 0).replace('<div class="sg', '<div data-k="' + k + '" class="sg'); }).join("")
                                  : input.value.trim() ? '<div class="sg muted">' + esc(H.noMatch) + "</div>" : "";
      box.style.display = box.innerHTML ? "block" : "none";
    }
    input.addEventListener("input", function () { clearTimeout(tm); tm = setTimeout(show, 60); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!list.length) return;
        moved = true;
        sel = Math.max(0, Math.min(list.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
        Array.prototype.forEach.call(box.querySelectorAll("[data-k]"), function (c) { c.classList.toggle("on", +c.dataset.k === sel); });
      } else if (e.key === "Enter") {
        clearTimeout(tm);
        if (!moved) list = suggest(input.value);
        var target = moved && list[sel] ? pickTarget(list[sel], mode) : typedTarget(input.value, list, mode);
        if (target) { box.style.display = "none"; location.hash = target; }
      } else if (e.key === "Escape") box.style.display = "none";
    });
    box.addEventListener("mousedown", function (e) {
      var r = e.target.closest("[data-k]");
      if (r && list[+r.dataset.k]) { e.preventDefault(); box.style.display = "none"; location.hash = pickTarget(list[+r.dataset.k], mode); }
    });
    input.addEventListener("blur", function () { setTimeout(function () { box.style.display = "none"; }, 150); });
  }

  // ── home ──
  function wide(on) { $("app").classList.toggle("wide", !!on); }
  function pageHome() {
    crumbs([]); toc("home"); wide(true);
    if (!HOME) { pageSystem(); return; }
    var S = HOME.summary, h = '<div class="home">';
    var stores = Object.keys(S.stores).map(function (k) { return k + " " + S.stores[k]; }).join(" · ");
    var epk = Object.keys(S.epk).map(function (k) { return k + " " + S.epk[k]; }).join(" · ");
    var cells = [
      ["repos", S.repos, hrefOf("system"), ""], ["services", S.services, hrefOf("system"), ""],
      ["data", S.data, hrefOf("t"), stores], ["eps", S.eps, hrefOf("find/eps"), epk],
      ["flows", S.flows, hrefOf("flow"), ""], ["unres", S.unres, hrefOf("find/unresolved"), ""],
    ];
    h += '<div class="strip" id="strip">' + cells.map(function (c) {
      return '<a class="stat' + (c[0] === "unres" ? " unres" + (c[1] ? " some" : "") : "") + '" data-stat="' + c[0] + '" href="' + c[2] + '"><b>' + num(c[1]) + "</b> " + esc(H[c[0]]) +
             (c[3] ? '<span class="sub">' + esc(c[3]) + "</span>" : "") + "</a>";
    }).join("") + "</div>";
    var ex = exampleRefs();
    h += '<div class="ask"><h1>' + esc(H.ask) + "</h1>" + askBox("hq", "ask") +
         (ex.length ? '<p class="muted small">' + esc(H.askHint) + ": " + ex.map(function (r) { return '<a href="' + hrefOf(qPath(r[0], "w")) + '"><code>' + esc(r[1]) + "</code></a>"; }).join(" · ") + "</p>" : "") + "</div>";
    h += "<h2>" + esc(H.finds) + '</h2><div class="finds" id="finds">' + cardsHtml() + "</div>";
    h += "<h2>" + esc(H.matrix) + '</h2><div id="mx"></div>';
    read(h + "</div>");
    detail("");
    wireAsk("hq", "ask");
    renderMatrix();
    var input = $("hq");
    if (input) input.focus();
  }
  function exampleRefs() {
    // a column written by more than one service, else the most written table
    var multi = HOME.cards.multi[0], out = [];
    var cols = columns().filter(function (c) { return c[5] > 1; }).sort(function (a, b) { return b[5] - a[5]; });
    if (cols.length) out.push([cols[0][0], cols[0][1]]);
    if (multi) out.push([multi[0], ents()[ENT_BY_REF[multi[0]]][2]]);
    else if (ents().length) { var top = ents().slice().sort(function (a, b) { return b[5] + b[6] - a[5] - a[6]; })[0]; out.push([top[0], top[2]]); }
    return out.slice(0, 2);
  }
  function entName(ref) { var k = ENT_BY_REF[ref]; return k === undefined ? ref : ents()[k][2]; }
  function card(kind, title, lead, body, n, href) {
    return '<div class="fcard' + (n ? "" : " zero") + '" data-card="' + kind + '" data-href="' + esc(href) + '"><h3>' + esc(title) + ' <span class="n">' + (n ? num(n) : esc(H.none)) + "</span></h3>" +
      (lead ? '<p class="muted small">' + esc(lead) + "</p>" : "") + (n ? body : "") + "</div>";
  }
  function cardsHtml() {
    var C = HOME.cards, out = [], lis;
    lis = C.unexpected.slice(0, 4).map(function (u) {
      var t = u[2].length === 1 ? entName(u[1]) + "." + u[2][0] : entName(u[1]);
      return '<li><a href="' + hrefOf(qPath(u[5], "w"), { svc: u[0] }) + '">' + esc(fmt(H.unexpectedLine, { s: sv(u[0]), t: t, o: sv(u[3]) })) + "</a></li>";
    }).join("");
    out.push(card("unexpected", H.unexpected, "", "<ul>" + lis + "</ul>", C.nunexpected, hrefOf("find/unexpected")));
    lis = C.multi.slice(0, 4).map(function (m) { return '<li><a href="' + hrefOf(qPath(m[0], "w")) + '">' + esc(fmt(H.multiLine, { t: entName(m[0]), n: m[1] })) + "</a></li>"; }).join("");
    out.push(card("multi", H.multi, "", "<ul>" + lis + "</ul>", C.nmulti, hrefOf("find/multi")));
    var dyn = C.dynamic.filter(function (d) { return d[1] || (DET.t && d[2]); });
    var ndyn = dyn.reduce(function (a, d) { return a + d[1] + (DET.t ? d[2] : 0); }, 0);
    out.push(card("dynamic", H.dynamic, H.dynamicLead, '<p class="svcs">' + dyn.map(function (d) {
      return '<a href="' + hrefOf("find/dynamic", { svc: d[0] }) + '">' + esc(sv(d[0])) + " <b>" + (d[1] + (DET.t ? d[2] : 0)) + "</b></a>";
    }).join(" · ") + "</p>", ndyn, hrefOf("find/dynamic")));
    var orp = C.orphans.filter(function (d) { return d[1] || (DET.t && d[2]); });
    var norp = orp.reduce(function (a, d) { return a + d[1] + (DET.t ? d[2] : 0); }, 0);
    out.push(card("orphans", H.orphans, H.orphansLead, '<p class="svcs">' + orp.map(function (d) {
      return '<a href="' + hrefOf("find/orphans", { svc: d[0] }) + '">' + esc(sv(d[0])) + " <b>" + (d[1] + (DET.t ? d[2] : 0)) + "</b></a>";
    }).join(" · ") + "</p>", norp, hrefOf("find/orphans")));
    if (C.nmirrors) out.push(card("mirrors", H.mirrors, "", '<p class="svcs">' + C.mirrors.map(function (m) { return esc(m[0] + " → " + m[1]) + " <b>" + m[2] + "</b>"; }).join(" · ") + "</p>",
                                  C.nmirrors, hrefOf("flow")));
    if (C.neverRead || C.neverWritten) out.push(card("never", H.never, "", "<p>" + esc(fmt(H.neverLine, { w: C.neverRead, r: C.neverWritten })) + "</p>",
                                                    C.neverRead + C.neverWritten, hrefOf("find/never")));
    return out.join("");
  }

  // ── matrix: services × tables / collections ──
  var MX = { q: "", repo: "", store: "", sort: "acc", limit: 30 };
  function renderMatrix() {
    var box = $("mx");
    if (!box) return;
    var all = ents();
    if (!all.length) { box.innerHTML = '<p class="muted">' + esc(H.empty) + "</p>"; return; }
    var repos = {}, stores = {};
    all.forEach(function (e) { e[4].forEach(function (r) { repos[r] = 1; }); stores[e[3]] = 1; });
    function cellOf(e, s) {
      for (var k = 0; k < e[7].length; k++) if (e[7][k][0] === s) {
        var c = e[7][k], rw = c[1] | (DET.t ? c[3] : 0), rank = Math.max(c[1] ? c[2] : 0, DET.t && c[3] ? c[4] : 0);
        return rw ? [rw, rank] : null;
      }
      return null;
    }
    var cols = all.filter(function (e) {
      return (!MX.q || e[2].toLowerCase().indexOf(MX.q) >= 0) && (!MX.repo || e[4].indexOf(MX.repo) >= 0) && (!MX.store || e[3] === MX.store);
    });
    var acc = function (e) { return e[5] + e[6] + (DET.t ? e[10] + e[11] : 0); };
    cols.sort(function (a, b) {
      if (MX.sort === "name") return a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0;
      if (MX.sort === "svc") return b[7].length - a[7].length || acc(b) - acc(a);
      return acc(b) - acc(a) || (a[2] < b[2] ? -1 : 1);
    });
    var shown = cols.slice(0, MX.limit);
    var rows = SV.map(function (s, i) { return i; }).filter(function (i) { return shown.some(function (e) { return cellOf(e, i); }); });
    var dyn = HOME.dyn || {};
    var h = '<div class="mx-bar"><input id="mx-q" class="filter" placeholder="' + esc(H.filterText) + '" value="' + esc(MX.q) + '"> <select id="mx-repo"><option value="">' + esc(H.anyRepo) + "</option>" +
      Object.keys(repos).sort().map(function (r) { return '<option' + (r === MX.repo ? " selected" : "") + ' value="' + esc(r) + '">' + esc(r) + "</option>"; }).join("") +
      '</select> <select id="mx-store"><option value="">' + esc(H.anyStore) + "</option>" + Object.keys(stores).sort().map(function (r) {
        return '<option' + (r === MX.store ? " selected" : "") + ' value="' + esc(r) + '">' + esc(r) + "</option>"; }).join("") +
      '</select> <select id="mx-sort">' + [["acc", H.sortAcc], ["svc", H.sortSvc], ["name", H.sortName]].map(function (o) {
        return '<option value="' + o[0] + '"' + (o[0] === MX.sort ? " selected" : "") + ">" + esc(o[1]) + "</option>"; }).join("") + '</select> <span class="muted small">' +
      esc(fmt(H.colsShown, { n: num(shown.length), t: num(cols.length) })) + "</span></div>";
    h += '<div class="mx-wrap"><table class="mx" id="mx-table"><thead><tr><th class="corner"></th>' + shown.map(function (e) {
      return '<th class="col" data-ref="' + esc(e[0]) + '" title="' + esc(e[2] + " · " + kindWordOf(e[1], e[3]) + " · " + e[3] + " · " + e[4].join(", ")) + '"><div><span class="mono">' + esc(e[2]) +
             '</span><span class="kind">' + esc(kindWordOf(e[1], e[3]) + " · " + e[3]) + "</span></div></th>";
    }).join("") + "</tr></thead><tbody>";
    rows.forEach(function (s) {
      var dy = dyn[s] || {};
      h += '<tr><th class="rowh"><a href="' + hrefOf("s/" + s) + '">' + esc(sv(s)) + "</a></th>" + shown.map(function (e) {
        var c = cellOf(e, s);
        if (!c) return '<td class="empty-c">—</td>';
        var fam = e[1] === "collection" ? "collection" : "table";
        var flag = dy[fam] ? ' <span class="dyn" title="' + esc(H.dynamic) + '">⚡</span>' : "";
        var tag = c[0] === 3 ? H.RW : c[0] & 2 ? H.W : H.R;
        return '<td class="c ' + (c[0] === 3 ? "rw" : c[0] & 2 ? "w" : "r") + (c[1] <= 2 ? " guess" : "") + '" data-ref="' + esc(e[0]) + '" data-svc="' + s + '" data-tab="' + (c[0] & 2 ? "w" : "r") + '">' + esc(tag) + flag + "</td>";
      }).join("") + "</tr>";
    });
    h += "</tbody></table></div>";
    if (cols.length > shown.length) h += '<p><button id="mx-more">' + esc(fmt(H.showMoreCols, { n: num(Math.min(30, cols.length - shown.length)), t: num(cols.length) })) + "</button></p>";
    h += '<p class="muted small">' + esc(H.cellHelp) + "</p>";
    box.innerHTML = h;
    var tq = null;
    $("mx-q").oninput = function (e) { clearTimeout(tq); var v = e.target.value; tq = setTimeout(function () { MX.q = v.trim().toLowerCase(); MX.limit = 30; renderMatrix(); var i = $("mx-q"); i.focus(); i.setSelectionRange(v.length, v.length); }, 150); };
    $("mx-repo").onchange = function (e) { MX.repo = e.target.value; MX.limit = 30; renderMatrix(); };
    $("mx-store").onchange = function (e) { MX.store = e.target.value; MX.limit = 30; renderMatrix(); };
    $("mx-sort").onchange = function (e) { MX.sort = e.target.value; renderMatrix(); };
    var more = $("mx-more");
    if (more) more.onclick = function () { MX.limit += 30; renderMatrix(); };
    $("mx-table").onclick = function (ev) {
      var td = ev.target.closest("td.c, th.col");
      if (!td) return;
      location.hash = td.tagName === "TD" ? hrefOf(qPath(td.dataset.ref, td.dataset.tab), { svc: td.dataset.svc }) : hrefOf(qPath(td.dataset.ref, "w"));
    };
  }

  // ── an answer: who writes / reads, and the trace ──
  function viaText(n) { return n === 1 ? H.via1 : fmt(H.viaN, { n: n }); }
  function nodeHref(b) {
    var m = meta(b.id);
    if (m.bi < 0) return null;
    if (b.type === "entrypoint") return hrefOf("r/" + m.bi);
    if (b.type === "function") return hrefOf("f/" + m.bi);
    return null;
  }
  function nodeA(b, cls) {
    var href = nodeHref(b);
    return href ? '<a class="' + (cls || "") + '" href="' + href + '">' + esc(b.label) + "</a>" : '<span class="' + (cls || "") + '">' + esc(b.label) + "</span>";
  }
  function chainHtml(entry, fn, full) {
    if (full) return entry.path.map(function (p) { return nodeA(p, p.type === "entrypoint" ? "ep" : ""); }).join(' <span class="arr">→</span> ');
    var parts = [nodeA(entry.entrypoint, "ep")];
    if (entry.handler && entry.handler.id !== fn.id) parts.push(nodeA(entry.handler));
    if (entry.via) parts.push('<button class="via" data-full="1" title="' + esc(H.fullPath) + '">' + esc(viaText(entry.via)) + "</button>");
    if (entry.entrypoint.id !== fn.id) parts.push(nodeA(fn, "writer"));
    return parts.join(' <span class="arr">→</span> ');
  }
  function answerFor(ref, access) {
    var ix = engine();
    return access === "trace" ? BizQuery.trace(ix, ref) : BizQuery.who(ix, ref, access === "w" || access === "writes" ? "writes" : "reads");
  }
  // the table record behind an answer (the ref the page uses, else the same name and kind)
  function tableRefOf(ref, e) {
    if (e.kind === "column") {
      var c = columns().filter(function (x) { return x[0] === ref; })[0] ||
              columns().filter(function (x) { return x[1].toLowerCase() === e.name.toLowerCase(); })[0];
      return c && c[4] ? c[4] : null;
    }
    if (ENT_BY_REF[ref] !== undefined) return ref;
    var hit = ents().filter(function (x) { return x[2] === e.name && x[1] === e.kind; })[0];
    return hit ? hit[0] : null;
  }
  function ownerOf(ref, res) {
    var tref = tableRefOf(ref, res.entity), k = tref !== null ? ENT_BY_REF[tref] : undefined;
    return k === undefined ? -1 : ents()[k][8];
  }
  function pageAnswer(ref, tab) {
    wide(true); toc("home");
    var ix = engine(), w, r;
    try { w = BizQuery.who(ix, ref, "writes"); r = BizQuery.who(ix, ref, "reads"); }
    catch (err) { if (err instanceof BizQuery.QueryError) return pageCandidates(ref, err); throw err; }
    var e = w.entity, svcF = PRM.svc !== undefined && PRM.svc !== "" ? +PRM.svc : null;
    crumbs(['<a href="' + hrefOf(qPath(ref, tab)) + '">' + esc(e.name) + "</a>"]);
    var h = '<div class="answer">' + askBox("aq", "ask", "") + "<h1>" + esc(e.name) + " " + kindStoreHtml(e.kind, Array.isArray(e.store) ? e.store.join(" / ") : e.store, e.repos) + "</h1>";
    var vis = function (res) { return res.results.filter(function (x) { var m = meta(x.function.id); return (DET.t || !m.hid) && (svcF === null || m.svc === svcF); }); };
    var tabs = [["w", H.writes, vis(w).length], ["r", H.reads, vis(r).length], ["t", H.trace, null]];
    h += '<div class="tabs atabs">' + tabs.map(function (t) {
      return '<a class="' + (t[0] === tab ? "on" : "") + '" data-tab="' + t[0] + '" href="' + hrefOf(qPath(ref, t[0]), { svc: svcF }) + '">' + esc(t[1]) + (t[2] !== null ? " (" + t[2] + ")" : "") + "</a>";
    }).join("") + '<span class="spacer"></span><a class="small" href="' + hrefOf("map/e/" + encodeURIComponent(mapRefOf(ref, e))) + '">' + esc(H.openMap) + "</a>";
    var tnode = e.nodes.map(function (n) { return meta(e.kind === "column" ? (ix.tableOfColumn(n.id) || n.id) : n.id).bi; }).filter(function (i) { return i >= 0; })[0];
    if (tnode !== undefined) h += ' · <a class="small" href="' + hrefOf("t/" + tnode + (e.kind === "column" ? "/" + encodeURIComponent(e.name.split(".").pop()) : "")) + '">' + esc(H.tablePage) + "</a>";
    h += "</div>";
    if (svcF !== null) h += '<p class="filterbar">' + esc(fmt(H.filteredBy, { s: sv(svcF) })) + ' · <a href="' + hrefOf(qPath(ref, tab)) + '">' + esc(H.clear) + "</a></p>";
    if (e.repos.length > 1) h += '<p class="muted small">' + esc(fmt(H.sameIn, { k: kindWordOf(e.kind, e.store), n: e.repos.length, r: e.repos.join(", ") })) + "</p>";
    var res = tab === "r" ? r : tab === "t" ? BizQuery.trace(ix, ref) : w;
    h += '<div id="ans">' + (tab === "t" ? traceHtml(res) : whoHtml(ref, res, svcF)) + "</div>";
    h += footerHtml(res) + "</div>";
    read(h); detail("");
    wireAsk("aq", "ask");
    $("ans").onclick = function (ev) {
      var b = ev.target.closest("[data-full]");
      if (!b) return;
      var row = b.closest(".arow");
      row.classList.toggle("full");
      ev.preventDefault();
    };
    var more = $("ans-more");
    if (more) more.onclick = function () { ANS_LIMIT += 100; route(); };
  }
  var ANS_LIMIT = 100;
  function mapRefOf(ref, e) { return tableRefOf(ref, e) || ref; }
  function whoHtml(ref, res, svcF) {
    var owner = ownerOf(ref, res), verb = res.access === "writes" ? H.writesVerb : H.readsVerb;
    var rows = [], hidden = 0;
    res.results.forEach(function (row) {
      var m = meta(row.function.id);
      if (svcF !== null && m.svc !== svcF) return;
      if (m.hid && !DET.t) { hidden++; return; }
      rows.push([row, m]);
    });
    if (!rows.length && !hidden) return '<p class="muted">' + esc(res.access === "writes" ? H.noneW : H.noneR) + "</p>";
    var h = "", lastRepo = null, shown = 0;
    rows.forEach(function (p) {
      if (shown >= ANS_LIMIT) return;
      var row = p[0], m = p[1], fn = row.function;
      if (row.repo !== lastRepo) { h += '<h3 class="repo">' + esc(row.repo || "-") + "</h3>"; lastRepo = row.repo; }
      var what = res.entity.name + (row.columns.length && res.entity.kind !== "column" ? " (" + row.columns.join(", ") + ")" : "");
      var odd = res.access === "writes" && owner >= 0 && m.svc >= 0 && m.svc !== owner;
      var entries = row.entrypoints.length ? row.entrypoints : [null];
      entries.forEach(function (entry) {
        if (shown >= ANS_LIMIT) return;
        shown++;
        var conf = entry ? entry.confidence : row.confidence;
        h += '<div class="arow' + (DET.i ? " full" : "") + (odd ? " odd" : "") + '">' +
          '<div class="chain short">' + (entry ? chainHtml(entry, fn, false) : nodeA(fn, "writer") + ' <span class="muted">(' + esc(H.noEntry) + ")</span>") +
          ' <span class="verb">' + esc(verb) + "</span> <b>" + esc(what) + "</b> " + confChip(conf) + "</div>" +
          (entry ? '<div class="chain long">' + chainHtml(entry, fn, true) + ' <span class="verb">' + esc(verb) + "</span> <b>" + esc(what) + "</b> " + confChip(conf) + "</div>" : "") +
          '<div class="ameta"><span class="ev">' + esc(row.evidence || "") + "</span> · " + esc(sv(m.svc)) +
          (odd ? ' <span class="badge odd">' + esc(H.unexpectedBadge) + '</span> <span class="muted small">' + esc(fmt(H.ownerIs, { o: sv(owner) })) + "</span>" : "") +
          (entry && entry.via ? ' · <a href="#" class="small" data-full="1">' + esc(H.fullPath) + " / " + esc(H.shortPath) + "</a>" : "") + "</div></div>";
      });
    });
    if (shown < rows.reduce(function (a, p) { return a + Math.max(1, p[0].entrypoints.length); }, 0)) h += '<p><button id="ans-more">+100</button></p>';
    if (hidden) h += '<p class="muted small hidden-note">' + esc(fmt(H.hiddenRows, { n: hidden })) + "</p>";
    return h;
  }
  function footerHtml(res) {
    var lines = BizQuery.footer(res, IX.lang);
    return '<div class="afoot" id="afoot"><b>' + esc(lines[0]) + "</b><ul>" + lines.slice(1).map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("") + "</ul></div>";
  }
  function refOf(b) {
    return '<span class="mono">' + esc(b.label) + "</span>" + (b.store ? ' <span class="kind">' + esc(kindWordOf(b.type, b.store) + " · " + b.store) + "</span>" : "") +
      (b.repo ? ' <span class="muted">' + esc(b.repo) + "</span>" : "");
  }
  function rootsText(id) {
    var ix = engine(), rows = ix.kind(id) === "entrypoint" ? [[{ root: id }], false] : BizQuery.roots(ix, id);
    return rows[0].slice(0, 3).map(function (r) { return '<span class="ep">' + esc(ix.label(r.root)) + "</span>" + (ix.repoOf(r.root) ? ' <span class="muted">' + esc(ix.repoOf(r.root)) + "</span>" : ""); }).join(", ");
  }
  function traceHtml(res) {
    var h = "";
    [["upstream", H.up, H.nothingUp], ["downstream", H.down, H.nothingDown]].forEach(function (sec) {
      var hops = res[sec[0]];
      h += '<h3 class="tsec" data-sec="' + sec[0] + '">' + esc(sec[1]) + "</h3>";
      if (!hops.length) { h += '<p class="muted">' + esc(sec[2]) + "</p>"; return; }
      var ordered = hops.slice().sort(function (a, b) { return a.depth - b.depth; }), lastD = 1;
      h += '<ul class="hops">';
      ordered.forEach(function (x) {
        if (x.depth !== lastD) { lastD = x.depth; h += '<li class="hopsep muted">' + esc(fmt(H.hops, { n: lastD })) + "</li>"; }
        var pad = ' style="margin-left:' + (x.depth - 1) * 18 + 'px"';
        if (x.kind === "writer") {
          var line = esc(H.writtenBy) + " " + nodeA(x.trigger, "ep") + (x.function.id !== x.trigger.id ? ' <span class="arr">→</span> ' + nodeA(x.function, "writer") : "") +
            (x.trigger.repo ? ' <span class="muted">[' + esc(x.trigger.repo) + "]</span>" : "") + ' <span class="muted">(' + refOf(x.into) + ")</span>";
          (x.queues || []).forEach(function (q) {
            line += '<div class="sub">' + esc(H.triggeredBy) + ' <span class="topic">' + esc(q.queue.name || q.queue.label) + "</span> ← " + esc(H.publishedBy) + " " +
              (q.producers.map(function (p) { return nodeA(p) + (p.repo ? ' <span class="muted">[' + esc(p.repo) + "]</span>" : "") + " ← " + rootsText(p.id); }).join("; ") || "?") + "</div>";
          });
          if (x.http_callers) line += '<div class="sub">' + esc(H.calledBy) + " " + x.http_callers.map(function (c) { return nodeA(c) + (c.repo ? ' <span class="muted">[' + esc(c.repo) + "]</span>" : ""); }).join(", ") + "</div>";
          h += '<li class="hop writer"' + pad + ">" + line + "</li>";
        } else if (x.kind === "feeds") {
          var cols = x.columns.length ? x.columns.slice(0, 6).map(function (c) { return esc(c.from + "→" + c.to); }).join(", ") + (x.columns.length > 6 ? " …" : "") : esc(H.tableLevel);
          h += '<li class="hop feeds"' + pad + ">" + esc(H.feeds) + ": " + refOf(x.from) + ' <span class="arr">→</span> ' + refOf(x.to) + " " + confChip(x.confidence) + ' <span class="muted small">' + cols +
               (x.via.length ? " · via " + x.via.map(function (v) { return nodeA(v); }).join(", ") : "") + "</span></li>";
        } else if (x.kind === "queue") {
          h += '<li class="hop queue"' + pad + ">" + esc(H.queue) + ' <span class="topic">' + esc(x.queue.name || x.queue.label) + "</span>: " + nodeA(x.trigger, "ep") + ' <span class="arr">→</span> ' +
               x.producers.map(function (p) { return nodeA(p); }).join(", ") + x.consumers.map(function (c) {
                 return '<div class="sub">' + esc(H.consumedBy) + " " + esc(c.repo || "-") + ": " + nodeA(c.consumer, "ep") + " " + confChip(c.confidence) +
                   (c.writes.length ? "; " + esc(H.thenWrites) + " " + c.writes.map(refOf).join(", ") : "") + "</div>";
               }).join("") + "</li>";
        } else if (x.kind === "http") {
          h += '<li class="hop http"' + pad + ">" + esc(H.http) + ": " + nodeA(x.caller) + ' <span class="arr">→</span> ' + nodeA(x.route, "ep") + " " + confChip(x.confidence) +
               (x.writes.length ? "; " + esc(H.thenWrites) + " " + x.writes.map(refOf).join(", ") : "") + "</li>";
        } else if (x.kind === "more_writers") {
          h += '<li class="hop"' + pad + ">" + esc(fmt(H.moreWriters, { n: x.count, t: x.into.label })) + "</li>";
        }
      });
      h += "</ul>";
    });
    return h;
  }
  function pageCandidates(text, err) {
    wide(true); toc("home");
    if (!err) {
      try { BizQuery.resolve(engine(), text); location.replace(hrefOf(qPath(text, "w"))); return; }
      catch (e2) { if (!(e2 instanceof BizQuery.QueryError)) throw e2; err = e2; }
    }
    crumbs([esc(text)]);
    var p = err.payload, h = '<div class="answer">' + askBox("aq", "ask", text);
    if (p.error === "ambiguous") {
      h += '<h1 class="amb">' + esc(fmt(H.ambiguous, { q: text, n: p.candidates.length })) + '</h1><ul class="cands" id="cands">' + p.candidates.map(function (c) {
        return '<li><a href="' + hrefOf(qPath(c.qualify, "w")) + '"><b class="mono">' + esc(c.name) + "</b> " + kindStoreHtml(c.kind, c.store, c.repos) + ' <code class="muted">' + esc(c.qualify) + "</code></a></li>";
      }).join("") + "</ul>";
    } else {
      h += "<h1>" + esc(fmt(H.notFound, { q: text })) + "</h1>";
      if (p.similar && p.similar.length) h += "<p>" + esc(H.similar) + " " + p.similar.map(function (s) { return '<a href="' + hrefOf("q", { n: s }) + '"><code>' + esc(s) + "</code></a>"; }).join(" · ") + "</p>";
    }
    read(h + "</div>"); detail("");
    wireAsk("aq", "ask");
  }

  // ── a topic: who publishes, who consumes, what they write ──
  function pageTopic(qid) {
    wide(true); toc("home");
    var ix = engine();
    if (!ix.has(qid)) return pageHome();
    var producers = Array.from(new Set(ix.incOf(qid).filter(function (e) { return e.type === "enqueues" && !e.derived; }).map(function (e) { return e.from; }))).sort();
    var hop = BizQuery.queueHop(ix, qid, producers, 1, qid, qid), name = hop.queue.name || hop.queue.label;
    crumbs([esc(name)]);
    var h = '<div class="answer">' + askBox("aq", "ask", "") + '<h1><span class="topic">' + esc(name) + '</span> <span class="kind">' + esc(H.kindTopic) + "</span></h1>" +
      '<p class="lead">' + esc(fmt(H.topicLead, { q: name })) + ' · <a href="' + hrefOf("map/q/" + encodeURIComponent(qid)) + '">' + esc(H.openMap) + "</a></p>";
    h += "<h3>" + esc(H.publishes) + "</h3><ul>" + hop.producers.map(function (p) { return "<li>" + nodeA(p) + (p.repo ? ' <span class="muted">[' + esc(p.repo) + "]</span>" : "") + " ← " + rootsText(p.id) + "</li>"; }).join("") + "</ul>";
    h += "<h3>" + esc(H.consumers) + "</h3>" + (hop.consumers.length ? "<ul>" + hop.consumers.map(function (c) {
      return "<li>" + esc(c.repo || "-") + ": " + nodeA(c.consumer, "ep") + " " + confChip(c.confidence) + (c.writes.length ? "; " + esc(H.thenWrites) + " " + c.writes.map(refOf).join(", ") : "") + "</li>";
    }).join("") + "</ul>" : '<p class="muted">' + esc(H.noConsumers) + "</p>");
    read(h + "</div>"); detail("");
    wireAsk("aq", "ask");
  }

  // ── findings lists ──
  function pageFind(kind) {
    wide(true); toc("home");
    var F = chunk("fx"), svcF = PRM.svc !== undefined && PRM.svc !== "" ? +PRM.svc : null, title, rows = [];
    function itemRow(it) {  // [label, evidence, svc, book index, hidden, detail, kind]
      if (it[4] && !DET.t) return null;
      if (svcF !== null && it[2] !== svcF) return null;
      var link = it[3] >= 0 ? '<a href="' + hrefOf((it[6] === "entrypoint" ? "r/" : "f/") + it[3]) + '">' + esc(it[0]) + "</a>" : esc(it[0]);
      return "<tr><td class=mono>" + link + '</td><td class="ev">' + esc(it[1]) + "</td><td>" + esc(sv(it[2])) + '</td><td class="muted">' + esc(it[5]) + "</td></tr>";
    }
    if (kind === "unexpected") {
      title = H.unexpected;
      rows = F.unexpected.map(function (u) {
        var t = u[2].length === 1 ? entName(u[1]) + "." + u[2][0] : entName(u[1]) + (u[2].length ? " (" + u[2].join(", ") + ")" : "");
        return '<tr><td><a href="' + hrefOf(qPath(u[5], "w"), { svc: u[0] }) + '">' + esc(fmt(H.unexpectedLine, { s: sv(u[0]), t: t, o: sv(u[3]) })) + '</a></td><td class="muted">' +
               esc(u[4] === "ddl" ? H.ownerDdl : H.ownerMost) + "</td></tr>";
      });
    } else if (kind === "multi") {
      title = H.multi;
      rows = F.multi.map(function (m) { return '<tr><td><a href="' + hrefOf(qPath(m[0], "w")) + '">' + esc(fmt(H.multiLine, { t: entName(m[0]), n: m[1] })) + "</a></td><td>" + esc(m[2].map(sv).join(", ")) + "</td></tr>"; });
    } else if (kind === "dynamic" || kind === "orphans" || kind === "unresolved") {
      title = kind === "dynamic" ? H.dynamic : kind === "orphans" ? H.orphans : H.unresTitle;
      rows = F[kind].map(itemRow).filter(function (x) { return x; });
    } else if (kind === "never") {
      title = H.never;
      rows = F.neverRead.map(function (r) { return '<tr><td><a href="' + hrefOf(qPath(r, "w")) + '">' + esc(entName(r)) + "</a></td><td>" + esc(H.writes) + "</td></tr>"; })
        .concat(F.neverWritten.map(function (r) { return '<tr><td><a href="' + hrefOf(qPath(r, "r")) + '">' + esc(entName(r)) + "</a></td><td>" + esc(H.reads) + "</td></tr>"; }));
    } else if (kind === "eps") {
      title = H.epsTitle;
      var nm = getNames();
      for (var i = 0; i < nm.t.length; i++) if (nm.t[i] === T_E) rows.push('<tr><td><a href="' + hrefOf("r/" + i) + '">' + esc(nm.l[i]) + "</a></td><td>" + esc(sv(nm.s[i])) + "</td></tr>");
    } else return pageHome();
    crumbs([esc(title)]);
    var limit = +(PRM.n || 300);
    var h = "<h1>" + esc(title) + ' <span class="muted">' + num(rows.length) + "</span></h1>" + (kind === "unresolved" ? '<p class="lead">' + esc(H.unresLead) + "</p>" : "") +
      (svcF !== null ? '<p class="filterbar">' + esc(fmt(H.filteredBy, { s: sv(svcF) })) + ' · <a href="' + hrefOf("find/" + kind) + '">' + esc(H.clear) + "</a></p>" : "") +
      '<table class="list" id="find-list"><tbody>' + rows.slice(0, limit).join("") + "</tbody></table>" +
      (rows.length > limit ? '<p><a href="' + hrefOf("find/" + kind, { svc: svcF, n: limit + 300 }) + '">+300</a></p>' : "");
    read(h); detail("");
  }

  // ── map: one entity (service, topic) and its neighbours, 1–2 hops ──
  var MG = null;
  function mapKind(n) { var p = n[3].split("·"); return kindWordOf(p[0], p[1]) + " · " + p[1]; }
  function mapGraph() {
    if (!MG) {
      MG = chunk("mg");
      MG.pos = {};
      MG.n.forEach(function (n, k) { MG.pos[n[0] + ":" + n[1]] = k; });
      MG.adj = MG.n.map(function () { return []; });
      MG.e.forEach(function (e, k) { MG.adj[e[0]].push(k); MG.adj[e[1]].push(k); });
    }
    return MG;
  }
  function pageMap2(kind, key) {
    wide(true); toc("map");
    crumbs([esc(H.mapTitle)]);
    var hops = PRM.h === "2" ? 2 : 1;
    if (!kind || IX.chunks.indexOf("mg") < 0) {
      read('<div class="answer"><h1>' + esc(H.mapTitle) + '</h1><p class="lead">' + esc(H.mapPick) + "</p>" + askBox("mq", "map", "") + "</div>");
      detail(""); wireAsk("mq", "map"); $("mq").focus();
      return;
    }
    var G = mapGraph(), center = G.pos[kind + ":" + key];
    if (center === undefined) { read('<p class="muted">' + esc(H.noEntity) + "</p>"); return; }
    var dist = {}, order = [center], qi = 0;
    dist[center] = 0;
    while (qi < order.length) {
      var u = order[qi++];
      if (dist[u] >= hops) continue;
      G.adj[u].forEach(function (k) {
        var e = G.e[k];
        if (e[5] && !DET.t) return;
        var v = e[0] === u ? e[1] : e[0];
        if (dist[v] === undefined) { dist[v] = dist[u] + 1; order.push(v); }
      });
    }
    var CAP = 150, keep = order.slice(0, CAP), keepSet = {};
    keep.forEach(function (k) { keepSet[k] = 1; });
    var els = keep.map(function (k) {
      var n = G.n[k];
      var lbl = n[0] === "e" ? n[2] + "\n" + mapKind(n) : n[0] === "q" ? n[2] + "\n" + H.kindTopic : n[2];
      return { data: { id: "n" + k, label: lbl, kind: n[0], center: k === center ? 1 : 0, level: hops + 1 - dist[k] } };
    });
    var relLabel = { feeds: H.feeds, pub: H.pubEdge, sub: H.subEdge, http: "HTTP" };
    G.e.forEach(function (e, k) {
      if (!keepSet[e[0]] || !keepSet[e[1]] || (e[5] && !DET.t)) return;
      var lab = e[2] === "rw" ? (e[3] === 3 ? H.RW : e[3] & 2 ? H.W : H.R) : relLabel[e[2]] || e[2];
      els.push({ data: { id: "e" + k, source: "n" + e[0], target: "n" + e[1], label: lab, guess: e[4] <= 2 ? 1 : 0, rel: e[2] } });
    });
    var c0 = G.n[center];
    // with details: the writer / reader functions and the columns of the centred table
    if (c0[0] === "e" && (DET.i || DET.c)) {
      var ix = engine(), extra = 0;
      ["writes", "reads"].forEach(function (acc) {
        var res;
        try { res = BizQuery.who(ix, c0[1], acc); } catch (err) { return; }
        res.results.forEach(function (row) {
          var m = meta(row.function.id);
          if ((m.hid && !DET.t) || extra > 60) return;
          var sk = G.pos["s:" + m.svc];
          if (DET.i && sk !== undefined && keepSet[sk]) {
            var fid = "f" + row.function.id;
            if (!els.some(function (x) { return x.data.id === fid; })) { els.push({ data: { id: fid, label: row.function.label, kind: "f", level: hops + 0.5 } }); extra++; }
            els.push({ data: { id: fid + acc + "s", source: "n" + sk, target: fid, label: "", rel: "own" } });
            els.push({ data: { id: fid + acc + "t", source: fid, target: "n" + center, label: acc === "writes" ? H.W : H.R, guess: BizQuery.rankOf(row.confidence) <= 2 ? 1 : 0, rel: "rw" } });
          }
          if (DET.c) row.columns.forEach(function (c) {
            var cid = "c" + c;
            if (!els.some(function (x) { return x.data.id === cid; })) { els.push({ data: { id: cid, label: c, kind: "c", level: hops + 0.5 } }); els.push({ data: { id: cid + "x", source: "n" + center, target: cid, label: "", rel: "col" } }); extra++; }
          });
        });
      });
    }
    if (DET.i) {  // a service's own link to the centre is now drawn through its functions
      var via = {};
      els.forEach(function (x) { if (x.data.rel === "own") via[x.data.source] = 1; });
      els = els.filter(function (x) { return !(x.data.rel === "rw" && x.data.target === "n" + center && via[x.data.source]); });
    }
    var h = '<div class="answer"><h1>' + esc(H.mapTitle) + ": " + esc(c0[2]) + ' <span class="kind">' + esc(c0[0] === "e" ? mapKind(c0) : c0[0] === "q" ? H.kindTopic : H.kindService) + "</span></h1>" +
      '<div class="tabs" id="hops"><a data-h="1" class="' + (hops === 1 ? "on" : "") + '" href="' + hrefOf("map/" + kind + "/" + encodeURIComponent(key), { h: 1 }) + '">' + esc(H.hop1) + "</a>" +
      '<a data-h="2" class="' + (hops === 2 ? "on" : "") + '" href="' + hrefOf("map/" + kind + "/" + encodeURIComponent(key), { h: 2 }) + '">' + esc(H.hop2) + "</a>" +
      (c0[0] === "e" ? '<a href="' + hrefOf(qPath(c0[1], "w")) + '">' + esc(H.writes) + " / " + esc(H.reads) + "</a>" : "") + "</div>" +
      (order.length > CAP ? '<p class="muted small">' + esc(fmt(H.mapCut, { n: CAP, t: order.length })) + "</p>" : "") + '<div id="map"></div></div>';
    read(h); detail("");
    window.__bizmap.mapNodes = els.filter(function (x) { return !x.data.source; }).map(function (x) { return x.data.label.split("\n")[0] + "|" + x.data.kind; });
    if (typeof cytoscape !== "function") return;
    var cy = cytoscape({ container: $("map"), elements: els, minZoom: 0.2, maxZoom: 2.5, boxSelectionEnabled: false,
      style: [
        { selector: "node", style: { "label": "data(label)", "shape": "round-rectangle", "width": 150, "height": 44, "text-wrap": "wrap", "text-valign": "center", "text-halign": "center",
          "font-size": 11, "background-color": "#eaeef2", "border-width": 1, "border-color": "#d0d7de", "color": "#1f2328" } },
        { selector: 'node[kind = "e"]', style: { "background-color": "#ddf4ff", "border-color": "#54aeff" } },
        { selector: 'node[kind = "s"]', style: { "background-color": "#0969da", "color": "#fff", "font-weight": 600 } },
        { selector: 'node[kind = "q"]', style: { "shape": "barrel", "background-color": "#fff8c5", "border-color": "#d4a72c" } },
        { selector: 'node[kind = "f"]', style: { "shape": "rectangle", "height": 28, "font-size": 10, "background-color": "#fff" } },
        { selector: 'node[kind = "c"]', style: { "shape": "ellipse", "width": 90, "height": 26, "font-size": 10, "background-color": "#f6f8fa" } },
        { selector: "node[center = 1]", style: { "border-width": 3, "border-color": "#cf222e" } },
        { selector: "edge", style: { "label": "data(label)", "curve-style": "bezier", "target-arrow-shape": "triangle", "width": 1.5, "font-size": 10, "line-color": "#8c959f",
          "target-arrow-color": "#8c959f", "text-background-color": "#fff", "text-background-opacity": 1 } },
        { selector: "edge[guess = 1]", style: { "line-style": "dashed" } },
      ],
      layout: { name: "concentric", concentric: function (n) { return n.data("level") || 0; }, levelWidth: function () { return 1; },
                minNodeSpacing: 40, animate: false } });
    cy.on("tap", "node", function (ev) {
      var id = ev.target.id();
      if (id.charAt(0) !== "n") return;
      var n = G.n[+id.slice(1)];
      location.hash = n[0] === "e" ? hrefOf(qPath(n[1], "w")) : n[0] === "s" ? hrefOf("s/" + n[1]) : hrefOf("topic/" + encodeURIComponent(n[1]));
    });
  }

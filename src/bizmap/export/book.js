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
      rule_other: "routes outside the patterns", rule_tag: "API tag", rule_kind: "non-HTTP entrypoints",
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
      generated: "Graph", domainsTitle: "Domains", screensTitle: "Screens", dataTitle: "Tables it reads and writes",
      apis: "APIs", workers: "Workers", libs: "Libraries", httpTitle: "HTTP calls it makes", files: "files", file1: "file",
      screensLeadFiles: "{n} files call the API (no UI files: each calling file is a screen).", screensLeadOne: "1 file calls the API (no UI files: it is the screen).",
      clientTitle: "API client code", clientFile: "API client", clientLead: "Files whose functions call an API (the client layer). Tests, mocks and generated code are left out.",
      screensN: "{n} screens call the API through its client code.", screensN1: "1 screen calls the API through its client code.",
      linkedN: "{l} of {n} client files are linked to a screen by the calls the scan follows.", linkedN1: "{l} of 1 client file is linked to a screen by the calls the scan follows.",
      linkedLead: "{n} of {t} client files are linked to a screen by the calls the scan follows (a function passed by reference, as to a hook, is not followed).",
      hiddenCalls: "{n} HTTP calls from tests, mocks or generated code are not counted.",
      hiddenCalls1: "1 HTTP call from tests, mocks or generated code is not counted.",
      noScreens: "No screen file reaches an API call through the calls the scan follows; see the API client code below.",
      noScreen: "no screen linked", noScreenLong: "No screen reaches this file through the calls the scan follows (for example when a function is passed by reference to a hook).",
      throughClient: "Through", usedOnScreens: "Used on screens", onScreens: "Screens", byFunctions: "Calling functions",
      guess: "likely", guessHint: "Dashed links are likely matches (a path found in the code matched to a route by its end); solid ones were matched from the full URL.",
      reasonHelp_target_not_scanned: "The full URL is known but no scanned route has that path: the service is not in the scan, or the route was renamed or removed.",
      reasonHelp_base_unknown: "The path is known but not the base URL (an environment variable no scanned service is mapped to).",
      reasonHelp_path_dynamic: "The path is built at runtime (template strings, concatenation) and could not be resolved.",
      reasonHelp_client_unknown: "An HTTP client the scan does not recognise.",
      moreDetails: "Screens, HTTP calls, tables and code of this service",
      endpoint: "endpoint", endpoints: "endpoints", openEndpoint: "Open an endpoint to see what it receives, returns, who uses it and the data it touches.",
      requestBody: "Request body (JSON)", responses: "Responses", noFields: "The fields of this type come from the API's swagger.json / openapi.json: export with it (see --openapi).",
      noDocs: "No API docs for this route yet (swag annotations or an OpenAPI file add the parameters and the JSON it receives and returns).",
      required: "required", example: "example", or: "or", writesTo: "Writes", readsFrom: "Reads", dataTitleShort: "Data",
      usesTitle: "API routes it uses", usesHeading: "API routes {s} uses", usesSummary: "{n} routes",
      usesLead: "{n} routes of the scanned APIs are called from this code, grouped by API and domain.", usesLead1: "1 route of the scanned APIs is called from this code.",
      calledFromFiles: "called from", unmatchedMore: "{n} more calls matched no scanned route", unmatchedMore1: "1 more call matched no scanned route",
      tabExample: "Example", tabFields: "Fields", colField: "Field", colType: "Type", colDesc: "Description", oneOf: "one of",
      seeCode: "See in code", hideCode: "Hide code", calledByN: "Called by {n} functions in other services (no screen linked).",
      calledByN1: "Called by 1 function in another service (no screen linked).",
      foldersTitle: "Code by folder", foldersRest: "Code no route reaches, by folder", colFolder: "Folder", colHttp: "HTTP calls", showMore: "Show {n} more (of {t})",
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
      rule_other: "rotas fora dos padrões", rule_tag: "tag da API", rule_kind: "entrypoints que não são HTTP",
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
      generated: "Grafo", domainsTitle: "Domínios", screensTitle: "Telas", dataTitle: "Tabelas que lê e escreve",
      apis: "APIs", workers: "Workers", libs: "Bibliotecas", httpTitle: "Chamadas HTTP que faz", files: "arquivos", file1: "arquivo",
      screensLeadFiles: "{n} arquivos chamam a API (sem arquivos de UI: cada arquivo que chama é uma tela).", screensLeadOne: "1 arquivo chama a API (sem arquivos de UI: ele é a tela).",
      clientTitle: "Código cliente da API", clientFile: "cliente da API", clientLead: "Arquivos cujas funções chamam uma API (a camada cliente). Testes, mocks e código gerado ficam de fora.",
      screensN: "{n} telas chamam a API pelo código cliente.", screensN1: "1 tela chama a API pelo código cliente.",
      linkedN: "{l} de {n} arquivos cliente estão ligados a uma tela pelas chamadas que o scan segue.", linkedN1: "{l} de 1 arquivo cliente está ligado a uma tela pelas chamadas que o scan segue.",
      linkedLead: "{n} de {t} arquivos cliente estão ligados a uma tela pelas chamadas que o scan segue (função passada por referência, como para um hook, não é seguida).",
      hiddenCalls: "{n} chamadas HTTP de testes, mocks ou código gerado não entram na contagem.",
      hiddenCalls1: "1 chamada HTTP de testes, mocks ou código gerado não entra na contagem.",
      noScreens: "Nenhum arquivo de tela chega a uma chamada de API pelas chamadas que o scan segue; veja o código cliente abaixo.",
      noScreen: "sem tela ligada", noScreenLong: "Nenhuma tela chega a este arquivo pelas chamadas que o scan segue (por exemplo, quando a função é passada por referência para um hook).",
      throughClient: "Por meio de", usedOnScreens: "Usado nas telas", onScreens: "Telas", byFunctions: "Funções que chamam",
      guess: "provável", guessHint: "Links tracejados são prováveis (um path achado no código casado com uma rota pelo final); os contínuos vieram da URL completa.",
      reasonHelp_target_not_scanned: "A URL completa é conhecida, mas nenhuma rota escaneada tem esse path: o serviço não está no scan ou a rota mudou ou foi removida.",
      reasonHelp_base_unknown: "O path é conhecido, mas não a URL base (uma variável de ambiente sem serviço mapeado).",
      reasonHelp_path_dynamic: "O path é montado em tempo de execução (template string, concatenação) e não pôde ser resolvido.",
      reasonHelp_client_unknown: "Um cliente HTTP que o scan não reconhece.",
      moreDetails: "Telas, chamadas HTTP, tabelas e código deste serviço",
      endpoint: "endpoint", endpoints: "endpoints", openEndpoint: "Abra um endpoint para ver o que ele recebe, o que devolve, quem usa e os dados que toca.",
      requestBody: "Corpo da requisição (JSON)", responses: "Respostas", noFields: "Os campos deste tipo vêm do swagger.json / openapi.json da API: exporte com ele (veja --openapi).",
      noDocs: "Esta rota ainda não tem documentação de API (anotações do swag ou um arquivo OpenAPI trazem os parâmetros e o JSON que ela recebe e devolve).",
      required: "obrigatório", example: "exemplo", or: "ou", writesTo: "Grava em", readsFrom: "Lê", dataTitleShort: "Dados",
      usesTitle: "Rotas da API que usa", usesHeading: "Rotas da API que {s} usa", usesSummary: "{n} rotas",
      usesLead: "{n} rotas das APIs escaneadas são chamadas por este código, agrupadas por API e domínio.", usesLead1: "1 rota das APIs escaneadas é chamada por este código.",
      calledFromFiles: "chamada em", unmatchedMore: "Mais {n} chamadas não ligaram em nenhuma rota escaneada", unmatchedMore1: "Mais 1 chamada não ligou em nenhuma rota escaneada",
      tabExample: "Exemplo", tabFields: "Campos", colField: "Campo", colType: "Tipo", colDesc: "Descrição", oneOf: "um de",
      seeCode: "Ver no código", hideCode: "Esconder código", calledByN: "Chamada por {n} funções de outros serviços (sem tela ligada).",
      calledByN1: "Chamada por 1 função de outro serviço (sem tela ligada).",
      foldersTitle: "Código por pasta", foldersRest: "Código que nenhuma rota alcança, por pasta", colFolder: "Pasta", colHttp: "Chamadas HTTP", showMore: "Mostrar mais {n} (de {t})",
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
  // a string with a singular variant (`key1`) when n is 1
  function plural(key, n, o) { o = o || {}; o.n = num(n); return fmt(n === 1 && L[key + "1"] ? L[key + "1"] : L[key], o); }
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
    var h = "";
    var groups = [["api", L.apis], ["web", L.frontends], ["worker", L.workers], ["lib", L.libs]];
    var dsvc = active && active.charAt(0) === "d" && DOM[+active.slice(1)] ? DOM[+active.slice(1)].svc : -1;
    groups.forEach(function (g) {
      var list = SV.map(function (sv, i) { return [sv, i]; }).filter(function (p) { return p[0].kind === g[0]; });
      if (!list.length) return;
      h += "<h4>" + esc(g[1]) + "</h4>";
      list.forEach(function (p) {
        var sv = p[0], i = p[1];
        var mine = active === "s" + i || active === "w" + i || active === "u" + i || active === "x" + i || dsvc === i;
        var n = sv.routes + sv.jobs ? num(sv.routes + sv.jobs) : sv.kind === "web" ? num(sv.screens) + " " + esc(sv.screens === 1 ? L.screen : L.screens) : num(sv.functions);
        h += '<a href="#/s/' + i + '" class="' + (active === "s" + i ? "on" : "") + '"><span>' + esc(sv.name) + '</span><span class="n">' + n + "</span></a>";
        if (!mine) return;
        sv.domains.forEach(function (d) {
          h += '<a class="sub ' + (active === "d" + d ? "on" : "") + '" href="#/d/' + d + '"><span>' + esc(DOM[d].name === "other" ? L.other : DOM[d].name) +
               '</span><span class="n">' + DOM[d].routes + "</span></a>";
        });
        if (sv.screen_chunk) h += '<a class="sub ' + (active === "w" + i ? "on" : "") + '" href="#/w/' + i + '"><span>' + esc(sv.kind === "web" ? L.screensTitle : L.httpTitle) + '</span><span class="n">' + num(sv.kind === "web" ? sv.screens : sv.clients) + "</span></a>";
        if (sv.uses) h += '<a class="sub ' + (active === "x" + i ? "on" : "") + '" href="#/x/' + i + '"><span>' + esc(L.usesTitle) + '</span><span class="n">' + num(sv.uses) + "</span></a>";
        if (sv.unmatched) h += '<a class="sub warn ' + (active === "u" + i ? "on" : "") + '" href="#/u/' + i + '"><span>⚠ ' + esc(L.unmatched) + '</span><span class="n">' + num(sv.unmatched) + "</span></a>";
      });
    });
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
        var href = "#/s/" + i;
        h += '<div class="card" data-href="' + href + '"><h3>' + (risk ? '<span class="risk">⚠</span>' : "") + esc(s.name) +
             ' <span class="kind">' + esc(L[s.kind] || s.kind) + '</span></h3><div class="stats">';
        if (s.routes + s.jobs) h += num(s.routes + s.jobs) + " " + esc(L.routes) + " · " + s.domains.length + " " + esc(s.domains.length === 1 ? L.domain1 : L.domains) + "<br>";
        if (s.kind === "web") h += num(s.screens) + " " + esc(s.screens === 1 ? L.screen : L.screens) + (s.unmatched ? ' · <span class="risk">' + num(s.unmatched) + " " + esc(L.unmatched) + "</span>" : "") + "<br>";
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
    crumbs(['<a href="#/s/' + s + '">' + esc(sv.name) + "</a>"]); toc(sv.kind === "web" && !sv.domains.length ? "w" + s : "s" + s);
    var h = "<h1>" + esc(sv.name) + ' <span class="kind">' + esc(L[sv.kind] || sv.kind) + '</span></h1><p class="lead">' +
            (sv.routes + sv.jobs ? num(sv.routes + sv.jobs) + " " + esc(L.routes) + " · " + sv.domains.length + " " + esc(sv.domains.length === 1 ? L.domain1 : L.domains) + " · " : "") +
            num(sv.functions) + " " + esc(L.functions) + " · " + num(sv.tables) + " " + esc(L.tablesLc) + "</p>";
    if (sv.domains.length) {
      h += "<h2>" + esc(L.domainsTitle) + '</h2><table class="list fixed"><colgroup><col style="width:34%"><col style="width:90px"><col><col style="width:80px"></colgroup><thead><tr><th>' + esc(L.domain) + '</th><th class="num">' + esc(L.endpoints) +
           "</th><th>" + esc(L.dataTitleShort) + '</th><th class="num">' + esc(L.colUsedBy) + "</th></tr></thead><tbody>";
      sv.domains.forEach(function (d) {
        var dm = DOM[d];
        h += '<tr class="row" data-href="#/d/' + d + '"><td><b>' + esc(dm.name === "other" ? L.other : dm.name) + '</b></td><td class="num">' + dm.routes + '</td><td class="muted ell">' +
             esc(dm.tables.slice(0, 4).map(function (p) { return IX.tableNames[p[0]]; }).join(", ") + (dm.ntables > 4 ? " +" + (dm.ntables - 4) : "")) +
             '</td><td class="num">' + (dm.consumers ? num(dm.consumers) : "—") + "</td></tr>";
      });
      h += "</tbody></table>";
      h += '<details class="more"><summary>' + esc(L.moreDetails) + "</summary>";
    }
    if (sv.uses) {
      var usesBy = {};
      chunk(sv.uses_chunk).uses.forEach(function (u) { var t = getNames().s[u.r]; usesBy[t] = (usesBy[t] || 0) + 1; });
      h += "<h2>" + esc(L.usesTitle) + '</h2><p><a href="#/x/' + s + '">' + esc(fmt(L.usesSummary, { n: num(sv.uses) })) + "</a> " +
           Object.keys(usesBy).map(function (t) { return '<a class="chip http" href="#/x/' + s + '">' + esc(svcName(+t)) + " · " + usesBy[t] + "</a>"; }).join("") + "</p>";
    }
    if (sv.screen_chunk) {
      var wc = chunk(sv.screen_chunk);
      if (sv.kind === "web") {
        var groups = {};
        // distinct routes and distinct calls with no route per folder (screens share client functions)
        wc.screens.forEach(function (sc) {
          var g = groups[sc.g] = groups[sc.g] || [0, {}, {}];
          g[0]++;
          sc.r.forEach(function (p) { g[1][p[0]] = 1; });
          sc.via.forEach(function (f) { var rec = wc.fns[f]; if (rec && rec[8].length) g[2][f] = rec[8].length; });
        });
        Object.keys(groups).forEach(function (k) { var u = 0, m = groups[k][2]; Object.keys(m).forEach(function (f) { u += m[f]; }); groups[k][2] = u; });
        h += "<h2>" + esc(L.screensTitle) + ' <span class="muted">· <a href="#/w/' + s + '">' + num(sv.screens) + " " + esc(sv.screens === 1 ? L.screen : L.screens) + "</a></span></h2>";
        h += '<table class="list"><thead><tr><th>' + esc(L.colFolder) + '</th><th class="num">' + esc(L.screens) + '</th><th class="num">' + esc(L.colRoutes) + '</th><th class="num">' + esc(L.colUnmatched) + "</th></tr></thead><tbody>";
        Object.keys(groups).sort().forEach(function (g) {
          h += '<tr class="row" data-href="#/w/' + s + '"><td class="mono">' + esc(g) + '</td><td class="num">' + groups[g][0] + '</td><td class="num">' + Object.keys(groups[g][1]).length +
               '</td><td class="num">' + (groups[g][2] ? '<span class="risk">' + groups[g][2] + "</span>" : "—") + "</td></tr>";
        });
        h += "</tbody></table>";
      }
      var targets = {};
      wc.clients.forEach(function (c) { c.r.forEach(function (p) { var t = getNames().s[p[0]]; targets[t] = (targets[t] || 0) + 1; }); });
      h += "<h2>" + esc(L.httpTitle) + ' <span class="muted">· <a href="#/w/' + s + '">' + num(sv.clients) + " " + esc(sv.clients === 1 ? L.file1 : L.files) + "</a>" +
           (sv.unmatched ? ' · <a class="risk" href="#/u/' + s + '">' + num(sv.unmatched) + " " + esc(L.unmatched) + "</a>" : "") + "</span></h2><p>" +
           Object.keys(targets).map(function (t) { return '<a class="chip http" href="#/s/' + t + '">→ ' + esc(svcName(+t)) + " · " + targets[t] + "</a>"; }).join("") + "</p>";
      if (sv.kind === "web" && sv.has_ui && sv.clients) h += '<p class="muted">' + esc(fmt(L.linkedLead, { n: num(sv.clients_linked), t: num(sv.clients) })) + "</p>";
      if (sv.hidden_calls) h += '<p class="muted">' + esc(plural("hiddenCalls", sv.hidden_calls)) + "</p>";
    }
    if (sv.data.length) {
      var first = sv.data.slice(0, 60), rest = sv.data.slice(60);
      h += "<h2>" + esc(L.dataTitle) + ' <span class="muted">· ' + num(sv.tables) + "</span></h2><div>" + first.map(function (p) { return rwChip(p[0], p[1]); }).join("") +
           (rest.length ? '<span id="more-data"><button class="chip" id="more-data-btn">+' + num(sv.tables - first.length) + "</button></span>" : "") + "</div>";
    }
    if (sv.folders.length) {
      h += "<h2>" + esc(sv.domains.length ? L.foldersRest : L.foldersTitle) + '</h2><table class="list"><thead><tr><th>' + esc(L.colFolder) + '</th><th class="num">' +
           esc(L.colFunctions) + '</th><th class="num">' + esc(L.colHttp) + "</th><th>" + esc(L.colTables) + "</th></tr></thead><tbody>";
      sv.folders.forEach(function (f, k) {
        h += '<tr class="row" data-href="#/p/' + s + "/" + k + '"><td class="mono">' + esc(f[0]) + '</td><td class="num">' + num(f[1]) + '</td><td class="num">' + (f[2] ? num(f[2]) : "—") +
             "</td><td>" + tableChips(f[4], 4) + (f[3] > f[4].length ? '<span class="chip">+' + (f[3] - f[4].length) + "</span>" : "") + "</td></tr>";
      });
      h += "</tbody></table>";
    }
    if (sv.domains.length) h += "</details>";
    if (!sv.domains.length && !sv.screen_chunk && !sv.data.length && !sv.folders.length) h += '<p class="empty">' + esc(L.nothing) + "</p>";
    read(h);
    var md = $("more-data-btn");
    if (md) md.onclick = function () {
      $("more-data").innerHTML = sv.data.slice(60).map(function (p) { return rwChip(p[0], p[1]); }).join("") +
        (sv.tables > sv.data.length ? '<span class="chip">+' + (sv.tables - sv.data.length) + "</span>" : "");
    };
    serviceDetail(s);
  }

  // a folder of a service's code: its functions, with data access and HTTP calls
  function pageFolder(s, k, limit) {
    var sv = SV[s], f = sv.folders[k];
    if (!f) return pageService(s);
    limit = limit || 300;
    crumbs(['<a href="#/s/' + s + '">' + esc(sv.name) + "</a>", '<span class="mono">' + esc(f[0]) + "</span>"]); toc("s" + s);
    var h = '<h1 class="mono">' + esc(f[0]) + '</h1><p class="lead">' + esc(sv.name) + " · " + num(f[1]) + " " + esc(L.functions) + "</p>";
    h += '<table class="list"><thead><tr><th>' + esc(L.function) + "</th><th>" + esc(L.colFile) + "</th><th>" + esc(L.colTables) + "</th><th>" + esc(L.colHttp) + "</th></tr></thead><tbody>";
    var shown = 0;
    for (var c = 0; c < f[5].length && shown < limit; c++) {
      var ch = chunk(f[5][c]);
      for (var j = 0; j < ch.items.length && shown < limit; j++, shown++) {
        var i = ch.items[j], rec = ch.fns[i] || fnRecord(i);
        var data = rec ? rec[6].map(function (d) { return rwChip(d[0], d[1]); }).join("") : "";
        var http = rec ? rec[7].slice(0, 3).map(function (e) { return '<a class="chip http" href="#/r/' + e + '">' + esc(label(e)) + "</a>"; }).join("") +
                         (rec[8].length ? '<span class="chip bad">⚠ ' + rec[8].length + "</span>" : "") : "";
        h += '<tr class="row" data-href="#/f/' + i + '"><td class="mono">' + esc(label(i, rec)) + '</td><td class="ev">' + esc(rec ? rec[1] : "") + "</td><td>" + data + "</td><td>" + http + "</td></tr>";
      }
    }
    h += "</tbody></table>";
    if (f[1] > shown) h += '<p><button id="more-fns">' + esc(fmt(L.showMore, { n: num(Math.min(f[1] - shown, 1000)), t: num(f[1]) })) + "</button></p>";
    read(h);
    var btn = $("more-fns");
    if (btn) btn.onclick = function () { var y = $("read").scrollTop; pageFolder(s, k, limit + 1000); $("read").scrollTop = y; };
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
    var h = "<h1>" + esc(name) + '</h1><p class="lead">' + esc(SV[s].name) + " · " + dm.routes + " " + esc(dm.routes === 1 ? L.endpoint : L.endpoints) + "</p>";
    h += '<div class="eps">';
    ch.routes.forEach(function (r) {
      var isHttp = /^[A-Z]+$/.test(r.m) && r.p.charAt(0) === "/";
      var sum = r.doc && r.doc.sum ? r.doc.sum : "";
      h += '<div class="ep' + (route === r.i ? " open" : "") + '" data-route="' + r.i + '"><div class="ep-head">' +
           (isHttp ? methodHtml(r.m) + '<span class="ep-path">' + pathHtml(r.p) + "</span>" : '<span class="kind">' + esc(r.m) + '</span> <span class="ep-path">' + esc(r.p) + "</span>") +
           '<span class="ep-sum">' + esc(sum) + "</span></div>" + (route === r.i ? '<div class="ep-body" id="ep-' + r.i + '"></div>' : "") + "</div>";
    });
    read(h + "</div>");
    if (route !== undefined && route !== null) {
      var r = ch.routes.filter(function (x) { return x.i === route; })[0];
      if (r) {
        $("ep-" + r.i).innerHTML = endpointHtml(r, dm);
        wireEndpoint(r, dm);
        routeDetail(r, dm);
        var row = document.querySelector('[data-route="' + r.i + '"]');
        if (row) row.scrollIntoView({ block: "nearest" });
      }
    } else detail('<p class="muted">' + esc(L.openEndpoint) + "</p>");
  }

  // ── an endpoint, read like API docs ──────────────────────────────────────
  var schemas = null;
  function schemaTable() {
    if (schemas === null) schemas = IX.chunks.indexOf("s0") >= 0 ? chunk("s0").schemas : {};
    return schemas;
  }
  function typeLabel(sc) {
    if (!sc) return "";
    if (sc.r) return sc.r;
    if (sc.t === "array") return typeLabel(sc.i) + "[]";
    return sc.t || "any";
  }
  // a schema shown like Swagger UI: an example JSON (default) and a table of fields
  function resolveRef(sc) { return sc && sc.r ? schemaTable()[sc.r] || null : sc; }
  function exampleOf(sc, seen, depth) {
    seen = seen || []; depth = depth || 0;
    if (!sc || depth > 7) return null;
    if (sc.r) {
      if (seen.indexOf(sc.r) >= 0) return {};
      var named = schemaTable()[sc.r];
      return named ? exampleOf(named, seen.concat([sc.r]), depth) : {};
    }
    if (sc.o) return exampleOf(sc.o[0], seen, depth);
    if (sc.x !== undefined) {
      var t0 = sc.t || "";
      if (/^(integer|number)/.test(t0) && !isNaN(+sc.x)) return +sc.x;
      if (/^boolean/.test(t0)) return sc.x === "true";
      return sc.x;
    }
    if (sc.e && sc.e.length) return sc.e[0];
    if (sc.t === "array") return [exampleOf(sc.i, seen, depth + 1)];
    if (sc.t === "object") {
      var o = {};
      (sc.f || []).forEach(function (f) { o[f[0]] = exampleOf(f[1], seen, depth + 1); });
      if (!sc.f || !sc.f.length) { if (sc.m) o.key = exampleOf(sc.m, seen, depth + 1); }
      return o;
    }
    var t = sc.t || "any";
    if (/date-time/.test(t)) return "2026-01-31T12:00:00Z";
    if (/\(date\)/.test(t)) return "2026-01-31";
    if (/uuid/.test(t)) return "3fa85f64-5717-4562-b3fc-2c963f66afa6";
    if (/email/.test(t)) return "user@example.com";
    if (/^(integer|number)/.test(t)) return 0;
    if (/^boolean/.test(t)) return true;
    if (/^string/.test(t)) return "string";
    return null;
  }
  function jsonHtml(v, ind) {
    ind = ind || "";
    var pad = ind + "  ";
    if (v === null) return '<span class="jn">null</span>';
    if (Array.isArray(v)) {
      if (!v.length) return '<span class="jp">[]</span>';
      return '<span class="jp">[</span>\n' + v.map(function (x) { return pad + jsonHtml(x, pad); }).join('<span class="jp">,</span>\n') + "\n" + ind + '<span class="jp">]</span>';
    }
    if (typeof v === "object") {
      var keys = Object.keys(v);
      if (!keys.length) return '<span class="jp">{}</span>';
      return '<span class="jp">{</span>\n' + keys.map(function (k) {
        return pad + '<span class="jk">"' + esc(k) + '"</span><span class="jp">: </span>' + jsonHtml(v[k], pad);
      }).join('<span class="jp">,</span>\n') + "\n" + ind + '<span class="jp">}</span>';
    }
    if (typeof v === "string") return '<span class="js">"' + esc(v) + '"</span>';
    if (typeof v === "boolean") return '<span class="jb">' + v + "</span>";
    return '<span class="jnum">' + esc(String(v)) + "</span>";
  }
  function typeName(sc) {
    if (!sc) return "";
    if (sc.r) return sc.r.split(".").pop();
    if (sc.t === "array") return typeName(sc.i) + "[]";
    if (sc.o) return sc.o.map(typeName).join(" | ");
    return sc.t || "any";
  }
  function fieldRows(sc, prefix, depth, seen, rows) {
    rows = rows || []; seen = seen || [];
    if (depth > 5) return rows;
    if (sc && sc.r) {
      if (seen.indexOf(sc.r) >= 0) return rows;
      seen = seen.concat([sc.r]);
    }
    var obj = resolveRef(sc);
    if (obj && obj.t === "array") return fieldRows(obj.i, prefix + "[]", depth, seen, rows);
    if (!obj || obj.t !== "object") return rows;
    (obj.f || []).forEach(function (f) {
      var name = (prefix ? prefix + "." : "") + f[0];
      var inner = resolveRef(f[1]);
      var notes = [];
      if (f[3]) notes.push(esc(f[3]));
      if (inner && inner.e) notes.push('<span class="muted">' + esc(L.oneOf) + ":</span> " + inner.e.map(function (x) { return "<code>" + esc(x) + "</code>"; }).join(" "));
      if (inner && inner.x !== undefined) notes.push('<span class="muted">' + esc(L.example) + ":</span> <code>" + esc(inner.x) + "</code>");
      rows.push('<tr><td class="fname" style="padding-left:' + (8 + depth * 14) + 'px">' + esc(name.split(".").pop()) + (f[2] ? '<span class="req" title="' + esc(L.required) + '">*</span>' : "") +
        '</td><td class="ftype">' + esc(typeName(f[1])) + "</td><td>" + notes.join("<br>") + "</td></tr>");
      fieldRows(f[1], name, depth + 1, seen, rows);
    });
    return rows;
  }
  var schemaSeq = 0;
  function schemaHtml(sc) {
    if (!sc) return "";
    var named = sc.r ? schemaTable()[sc.r] : null;
    if (sc.r && !named) return '<div class="schema-box"><div class="schema-tabs"><span class="schema-name">' + esc(sc.r) + '</span></div><p class="muted small schema-note">' + esc(L.noFields) + "</p></div>";
    var id = "sch" + (schemaSeq++);
    var rows = fieldRows(sc, "", 0, []);
    var ex = exampleOf(sc);
    var title = typeName(sc);
    return '<div class="schema-box" id="' + id + '"><div class="schema-tabs"><span class="schema-name">' + esc(title) + "</span>" +
      '<button class="stab on" data-tab="ex" data-box="' + id + '">' + esc(L.tabExample) + "</button>" +
      (rows.length ? '<button class="stab" data-tab="fields" data-box="' + id + '">' + esc(L.tabFields) + "</button>" : "") + "</div>" +
      '<pre class="json" data-pane="ex">' + jsonHtml(ex) + "</pre>" +
      (rows.length ? '<table class="fields" data-pane="fields" hidden><thead><tr><th>' + esc(L.colField) + "</th><th>" + esc(L.colType) + "</th><th>" + esc(L.colDesc) +
                     "</th></tr></thead><tbody>" + rows.join("") + "</tbody></table>" : "") + "</div>";
  }
  function dataSentence(list) {
    var w = list.filter(function (p) { return p[1] & W; }).map(function (p) { return '<a href="#/t/' + p[0] + '">' + esc(IX.tableNames[p[0]] || label(p[0])) + "</a>"; });
    var r = list.filter(function (p) { return p[1] & R; }).map(function (p) { return '<a href="#/t/' + p[0] + '">' + esc(IX.tableNames[p[0]] || label(p[0])) + "</a>"; });
    var out = [];
    if (w.length) out.push(esc(L.writesTo) + " " + w.join(", ") + ".");
    if (r.length) out.push(esc(L.readsFrom) + " " + r.join(", ") + ".");
    return out.join(" ");
  }
  function endpointHtml(r, dm) {
    var doc = r.doc || {}, ch = chunk(dm.chunk), h = "";
    var desc = doc.desc || (doc.src === "comment" ? "" : "");
    if (desc) h += '<p class="ep-desc">' + esc(desc) + "</p>";
    // parameters: the docs' own, plus path parameters they don't mention
    var params = (doc.params || []).slice();
    (r.pp || []).forEach(function (p) { if (!params.some(function (x) { return x[0] === p; })) params.push([p, "path", "", 1, ""]); });
    if (params.length) {
      h += "<h4>" + esc(L.params) + '</h4><table class="params"><tbody>' + params.map(function (p) {
        return '<tr><td class="pname">' + esc(p[0]) + (p[3] ? '<span class="req">*</span>' : "") + '</td><td class="muted">' + esc(p[1]) + (p[2] ? " · " + esc(p[2]) : "") + "</td><td>" + esc(p[4] || "") + "</td></tr>";
      }).join("") + "</tbody></table>";
    }
    if (doc.body) h += "<h4>" + esc(L.requestBody) + "</h4>" + schemaHtml(doc.body);
    if (doc.resp && doc.resp.length) {
      h += "<h4>" + esc(L.responses) + "</h4>" + doc.resp.map(function (x) {
        var ok = /^2/.test(x[0]);
        return '<div class="resp"><span class="rcode ' + (ok ? "ok" : /^[45]/.test(x[0]) ? "bad" : "") + '">' + esc(x[0]) + "</span> " + esc(x[1] || "") +
               (x[2] && (ok || x[2].r) && (x[2].r || x[2].f || x[2].i) ? schemaHtml(x[2]) : "") + "</div>";
      }).join("");
    }
    if (!doc.src || doc.src === "comment") h += '<p class="muted small">' + esc(L.noDocs) + "</p>";
    // who uses it: screens by a human name
    if (r.sc.length) {
      h += "<h4>" + esc(L.usedOnScreens) + "</h4><div>" + r.sc.map(function (p) {
        return '<span class="chip screen' + (p[2] ? " guess" : "") + '" title="' + esc(p[1] + (p[2] ? " — " + L.guess : "")) + '">' + esc(humanFile(p[1])) + ' <span class="muted">' + esc(svcName(p[0])) + "</span>" + (p[2] ? " · " + esc(L.guess) : "") + "</span>";
      }).join("") + "</div>";
    } else if (r.nc) {
      h += "<h4>" + esc(L.consumers) + '</h4><p class="muted">' + esc(plural("calledByN", r.nc)) + "</p>";
    }
    if (r.t.length) h += "<h4>" + esc(L.dataTitleShort) + '</h4><p class="ep-data">' + dataSentence(r.t) + "</p>";
    h += '<p><button class="code-btn" data-code="' + r.i + '">' + esc(L.seeCode) + ' ▾</button></p><div class="tree code" id="code-' + r.i + '" hidden></div>';
    return h;
  }
  function wireEndpoint(r, dm) {
    var body = $("ep-" + r.i);
    body.onclick = function (ev) {
      var t = ev.target;
      if (t.classList.contains("stab")) {
        var box = $(t.dataset.box);
        Array.prototype.forEach.call(box.querySelectorAll(".stab"), function (b) { b.classList.toggle("on", b === t); });
        Array.prototype.forEach.call(box.querySelectorAll("[data-pane]"), function (p) { p.hidden = p.dataset.pane !== t.dataset.tab; });
        ev.stopPropagation();
        return;
      }
      if (t.dataset && t.dataset.code) {
        var box = $("code-" + r.i);
        box.hidden = !box.hidden;
        if (!box.hidden && !box.children.length) renderTree(box, r.h, dm.chunk);
        t.textContent = (box.hidden ? L.seeCode + " ▾" : L.hideCode + " ▴");
        ev.stopPropagation();
        return;
      }
      ev.stopPropagation();
    };
  }
  function humanFile(f) {
    var parts = f.split("/"), base = parts.pop().replace(/\.[^.]+$/, "");
    if (/^(index|page|route|main)$/i.test(base) && parts.length) base = parts.pop();
    base = base.replace(/(Page|Screen|View|Container|Route)$/, "") || base;
    return base.replace(/[-_]+/g, " ").replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/^./, function (c) { return c.toUpperCase(); }).trim();
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
    var ch = chunk(dm.chunk);
    var h = "<h2>" + (/^[A-Z]+$/.test(r.m) ? methodHtml(r.m) + pathHtml(r.p) : esc(r.m + " " + r.p)) + "</h2>";
    h += '<p class="muted">' + esc(SV[dm.svc].name) + " › " + esc(dm.name === "other" ? L.other : dm.name) + "</p>";
    if (r.h.length) h += "<h3>" + esc(L.handler) + "</h3><ul>" + r.h.map(function (f) { var rec = ch.fns[f]; return "<li>" + fnLink(f) + (rec ? ' <div class="ev">' + esc(rec[1]) + "</div>" : "") + "</li>"; }).join("") + "</ul>";
    // the raw comment only when the route has no docs (it would repeat them, annotations included)
    if (r.why && !(r.doc && (r.doc.src === "swag" || r.doc.src === "spec"))) h += '<div class="why">' + esc(r.why.split(/\s@(?=[A-Z]\w+\b)/)[0]) + "</div>";
    h += "<h3>" + esc(L.consumers) + " (" + num(r.nc) + ")</h3>";
    if (!r.nc) h += '<p class="muted">' + esc(L.noConsumers) + "</p>";
    else {
      var nm = getNames();
      if (r.sc.length) {
        h += '<p class="muted">' + esc(L.onScreens) + "</p><ul>" + r.sc.map(function (p) {
          return '<li>🖥 <span class="mono">' + esc(p[1].split("/").pop()) + '</span> <span class="muted">' + esc(svcName(p[0])) + "</span>" + (p[2] ? ' <span class="chip guess">' + esc(L.guess) + "</span>" : "") + '<div class="ev">' + esc(p[1]) + "</div></li>";
        }).join("") + "</ul>";
      }
      h += '<p class="muted">' + esc(L.byFunctions) + "</p><ul>" + r.c.map(function (p) {
        var f = p[0], rec = fnRecord(f), s = nm.s[f];
        var file = rec ? rec[1].replace(/:\d+$/, "") : "";
        return "<li>" + fnLink(f) + ' <span class="muted">' + esc(svcName(s)) + "</span>" + (p[1] ? ' <span class="chip guess">' + esc(L.guess) + "</span>" : "") + (file ? '<div class="ev">' + esc(file) + "</div>" : "") + "</li>";
      }).join("") + (r.nc > r.c.length ? '<li class="muted">+' + (r.nc - r.c.length) + " " + esc(L.more) + "</li>" : "") + "</ul>";
    }
    if (r.nh) h += '<p class="muted">' + esc(plural("hiddenCalls", r.nh)) + "</p>";
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
  function routeChips(list, max) {
    max = max || 6;
    return list.slice(0, max).map(function (p) {
      return '<a class="chip http' + (p[1] ? " guess" : "") + '" href="#/r/' + p[0] + '" title="' + esc(p[1] ? L.guess : "") + '">' + esc(label(p[0])) + "</a>";
    }).join("") + (list.length > max ? '<span class="chip">+' + (list.length - max) + "</span>" : "");
  }
  function fileTable(rows, key, extraHead, extraCell) {
    var groups = {};
    rows.forEach(function (sc, k) { (groups[sc.g] = groups[sc.g] || []).push(k); });
    var h = '<table class="list fixed"><colgroup><col style="width:28%"><col><col style="width:90px"></colgroup><thead><tr><th>' + esc(L.colFile) + "</th><th>" +
            esc(extraHead) + '</th><th class="num">' + esc(L.colUnmatched) + "</th></tr></thead><tbody>";
    Object.keys(groups).sort().forEach(function (g) {
      h += '<tr class="group"><td colspan="3" class="mono">' + esc(g) + "</td></tr>";
      groups[g].forEach(function (k) {
        var sc = rows[k];
        h += '<tr class="row" data-' + key + '="' + k + '"><td class="mono screen" title="' + esc(sc.f) + '">' + esc(sc.f.split("/").pop()) + '</td><td class="routes">' + extraCell(sc) +
             '</td><td class="num">' + (sc.u ? '<span class="risk">' + sc.u + "</span>" : "—") + "</td></tr>";
      });
    });
    return h + "</tbody></table>";
  }
  function pageScreens(s) {
    var sv = SV[s], ch = chunk(sv.screen_chunk);
    crumbs(['<a href="#/s/' + s + '">' + esc(sv.name) + "</a>", esc(sv.kind === "web" ? L.screensTitle : L.httpTitle)]); toc("w" + s);
    var h = "<h1>" + esc(sv.name) + ' <span class="kind">' + esc(L[sv.kind] || sv.kind) + "</span></h1>";
    if (sv.kind === "web") {
      h += '<p class="lead">' + esc(sv.has_ui ? plural("screensN", sv.screens) + " " + plural("linkedN", sv.clients, { l: num(sv.clients_linked) })
                                          : fmt(sv.screens === 1 ? L.screensLeadOne : L.screensLeadFiles, { n: num(sv.screens) })) + "</p>";
      h += "<h2>" + esc(L.screensTitle) + "</h2>" + (ch.screens.length ? fileTable(ch.screens, "screen", L.usedByScreens, function (sc) { return routeChips(sc.r); }) : '<p class="muted">' + esc(L.noScreens) + "</p>");
    }
    h += "<h2>" + esc(L.clientTitle) + '</h2><p class="muted">' + esc(L.clientLead) + "</p>";
    h += fileTable(ch.clients, "client", L.usedByScreens, function (c) {
      return routeChips(c.r) + (sv.kind === "web" && sv.has_ui && !c.ui.length && !/\.(tsx|jsx|vue|svelte)$/.test(c.f) ? ' <span class="chip">' + esc(L.noScreen) + "</span>" : "");
    });
    if (sv.hidden_calls) h += '<p class="muted">' + esc(plural("hiddenCalls", sv.hidden_calls)) + "</p>";
    read(h);
    detail('<p class="muted">' + esc(L.guessHint) + "</p>");
    $("read").onclick = function (ev) {
      if (ev.target.closest("a")) return;
      var tr = ev.target.closest("tr[data-screen], tr[data-client]");
      if (!tr) return;
      Array.prototype.forEach.call(document.querySelectorAll("tr.on"), function (x) { x.classList.remove("on"); });
      tr.classList.add("on");
      if (tr.dataset.screen !== undefined) screenDetail(s, ch.screens[+tr.dataset.screen], true);
      else screenDetail(s, ch.clients[+tr.dataset.client], false);
    };
  }
  function unmatchedHtml(rec) {
    return rec && rec[8].length ? rec[8].map(function (u) { return '<div><span class="chip bad">⚠ ' + esc(u[0] || "?") + "</span> <code>" + esc(u[1]) + '</code> <span class="muted">' + esc(L["reason_" + u[2]] || u[2]) + "</span></div>"; }).join("") : "";
  }
  function screenDetail(s, sc, isScreen) {
    var ch = chunk(SV[s].screen_chunk);
    var h = "<h2>" + esc(sc.f.split("/").pop()) + ' <span class="kind">' + esc(isScreen ? L.screen : L.clientFile) + '</span></h2><p class="ev">' + esc(sc.f) + "</p>";
    h += "<h3>" + esc(L.usedByScreens) + " (" + sc.r.length + ")</h3><ul>" + sc.r.map(function (p) { return "<li>" + routeLink(p[0]) + (p[1] ? ' <span class="chip guess">' + esc(L.guess) + "</span>" : "") + "</li>"; }).join("") + "</ul>";
    if (isScreen) {
      h += "<h3>" + esc(L.throughClient) + " (" + sc.via.length + ")</h3><ul>" + sc.via.map(function (f) { var rec = ch.fns[f]; return "<li>" + fnLink(f) + (rec ? '<div class="ev">' + esc(rec[1]) + "</div>" : "") + unmatchedHtml(rec) + "</li>"; }).join("") + "</ul>";
    } else {
      if (sc.ui.length) h += "<h3>" + esc(L.usedOnScreens) + " (" + sc.ui.length + ")</h3><ul>" + sc.ui.map(function (f) { return '<li class="ev">' + esc(f) + "</li>"; }).join("") + "</ul>";
      else if (SV[s].kind === "web" && SV[s].has_ui) h += '<p class="muted">' + esc(L.noScreenLong) + "</p>";
      h += "<h3>" + esc(L.functions) + " (" + sc.fns.length + ")</h3><ul>" + sc.fns.map(function (f) { return "<li>" + fnLink(f) + unmatchedHtml(ch.fns[f]) + "</li>"; }).join("") + "</ul>";
    }
    detail(h);
  }
  // the API routes a service uses, by API and domain: "web uses these routes of api"
  function pageUses(s) {
    var sv = SV[s], ch = chunk(sv.uses_chunk), nm = getNames();
    crumbs(['<a href="#/s/' + s + '">' + esc(sv.name) + "</a>", esc(L.usesTitle)]); toc("x" + s);
    var h = "<h1>" + esc(fmt(L.usesHeading, { s: sv.name })) + '</h1><p class="lead">' + esc(plural("usesLead", ch.uses.length)) + "</p>";
    var bySvc = {};
    ch.uses.forEach(function (u) { var t = nm.s[u.r]; (bySvc[t] = bySvc[t] || []).push(u); });
    Object.keys(bySvc).sort(function (a, b) { return bySvc[b].length - bySvc[a].length; }).forEach(function (t) {
      var rows = bySvc[t], byDom = {};
      rows.forEach(function (u) { var d = IX.routeDomain[u.r]; (byDom[d] = byDom[d] || []).push(u); });
      h += '<h2><a href="#/s/' + t + '">' + esc(svcName(+t)) + '</a> <span class="muted">· ' + rows.length + " " + esc(rows.length === 1 ? L.endpoint : L.endpoints) + "</span></h2>";
      h += '<div class="eps">';
      Object.keys(byDom).sort(function (a, b) { return (DOM[a] ? DOM[a].name : "").localeCompare(DOM[b] ? DOM[b].name : ""); }).forEach(function (d) {
        var dname = DOM[d] ? (DOM[d].name === "other" ? L.other : DOM[d].name) : "";
        h += '<div class="ep-group"><a href="#/d/' + d + '">' + esc(dname) + "</a></div>";
        byDom[d].forEach(function (u) {
          var lbl = nm.l[u.r], k = lbl.indexOf(" ");
          var who = u.ui.length ? u.ui.map(function (f) { return '<span class="chip screen" title="' + esc(f) + '">' + esc(humanFile(f)) + "</span>"; }).join("") +
                    (u.nui > u.ui.length ? '<span class="chip">+' + (u.nui - u.ui.length) + "</span>" : "")
                  : u.files.map(function (f) { return '<span class="chip" title="' + esc(f) + '">' + esc(f.split("/").slice(-2).join("/")) + "</span>"; }).join("");
          h += '<div class="ep use' + (u.g ? " guess" : "") + '"><div class="ep-head" data-href="#/r/' + u.r + '">' + (k > 0 ? methodHtml(lbl.slice(0, k)) + '<span class="ep-path">' + pathHtml(lbl.slice(k + 1)) + "</span>" : esc(lbl)) +
               '<span class="ep-sum">' + esc(u.s) + (u.g ? ' <span class="chip guess">' + esc(L.guess) + "</span>" : "") + '</span></div><div class="ep-who"><span class="muted">' + esc(u.ui.length ? L.usedOnScreens : L.calledFromFiles) + ":</span> " + who + "</div></div>";
        });
      });
      h += "</div>";
    });
    if (sv.unmatched) h += '<p class="muted" style="margin-top:16px"><a href="#/u/' + s + '">' + esc(plural("unmatchedMore", sv.unmatched)) + "</a></p>";
    read(h); detail('<p class="muted">' + esc(L.guessHint) + "</p>");
  }
  function pageUnmatched(s) {
    var sv = SV[s], ch = chunk(sv.screen_chunk);
    crumbs(['<a href="#/s/' + s + '">' + esc(sv.name) + "</a>", esc(L.unmatched)]); toc("u" + s);
    var byReason = {};
    ch.clients.forEach(function (c) {
      c.fns.forEach(function (f) { var rec = ch.fns[f]; if (rec) rec[8].forEach(function (u) { (byReason[u[2]] = byReason[u[2]] || []).push([f, u, c.f]); }); });
    });
    var h = "<h1>⚠ " + esc(L.unmatched) + '</h1><p class="lead">' + esc(L.unmatchedLead) + "</p>";
    Object.keys(byReason).sort(function (a, b) { return byReason[b].length - byReason[a].length; }).forEach(function (r) {
      h += "<h2>" + esc(L["reason_" + r] || r) + " (" + byReason[r].length + ')</h2><p class="muted">' + esc(L["reasonHelp_" + r] || "") + '</p><table class="list fixed"><colgroup><col style="width:42%"><col style="width:30%"><col></colgroup><tbody>' + byReason[r].slice(0, 1000).map(function (x) {
        return '<tr class="row" data-href="#/f/' + x[0] + '"><td>' + '<span class="chip bad">' + esc(x[1][0] || "?") + '</span> <code class="wrap">' + esc(x[1][1]) + '</code></td><td class="mono ell">' + esc(label(x[0])) + '</td><td class="ev">' + esc(x[2]) + "</td></tr>";
      }).join("") + "</tbody></table>";
    });
    if (sv.hidden_calls) h += '<p class="muted">' + esc(plural("hiddenCalls", sv.hidden_calls)) + "</p>";
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
    cy.on("tap", "node", function (e) { var i = +e.target.id().slice(1); location.hash = "#/s/" + i; });
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
    var head = ev.target.closest(".ep-head");
    var tr = head && head.parentNode;
    if (tr && tr.dataset.route !== undefined) {
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
      else if (h[0] === "p" && SV[+h[1]]) pageFolder(+h[1], +h[2]);
      else if (h[0] === "x" && SV[+h[1]] && SV[+h[1]].uses_chunk) pageUses(+h[1]);
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

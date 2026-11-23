  // ══ system views: architecture (#/architecture), event sources (#/queues) ══
  // and the flow of one topic / trigger / job (#/topic/<key>). Inlined into
  // book.js after home.js (same scope). Elements and positions come from the
  // export (bizmap/views.py: `ar` and `ev` chunks); Cytoscape only draws them
  // (preset layout), so the same graph always gives the same drawing.
  var VT = {
    en: {
      arch: "Architecture", archLead: "How services connect to stores and queues: front ends → APIs → topics → workers / functions → stores. Line width = number of accesses; dashed = only heuristic links. Click a box or a line.",
      queues: "Queues and events", queuesLead: "Every topic, Firestore trigger and scheduled job: who publishes, who consumes, and what is missing.",
      layers: ["Front ends", "APIs / services", "Topics", "Workers / functions", "Stores"],
      flowCols: ["Publisher reads", "Publishes", "Event", "Consumers", "Writes"], next: "Next: event", nextC: "Next: consumers", nextW: "Next: writes",
      st_ok: "ok", st_no_consumer: "no consumer", st_no_publisher: "no publisher",
      k_topic: "topic", k_firestore: "Firestore trigger", k_cron: "scheduled job",
      w_no_consumer: "Nothing in the scanned repos consumes {q}: messages published here are not read by any scanned service.",
      w_no_publisher: "Nothing in the scanned repos publishes to {q}: its consumers only run if something outside these repos sends to it.",
      w_cross: "Links services: {a} → {b}.", w_fields: "Read by the consumer but sent by no publisher: {f}.",
      w_no_writer: "Nothing in the scanned repos writes {q}: the trigger only fires on writes from outside these repos.",
      fields: "Message", fPub: "Published", fRead: "Read by the consumer", fMissing: "never published", fNone: "The scan did not see the message fields.",
      publishers: "Publishers", writers: "Writers", consumers: "Consumers", schedule: "Schedule", noOne: "none in the scanned repos", entry: "from", writesW: "writes",
      source: "Source", kind: "Kind", status: "Status", all: "All", problems: "Problems", topics: "Topics", triggers: "Triggers", jobs: "Jobs",
      accesses1: "1 access", accesses: "{n} accesses", calls1: "1 call", calls: "{n} calls", more: "+{n} more",
      store: "{n} tables / collections", expand: "Show its tables in the diagram", collapse: "Hide its tables", tooMany: "Too many to draw ({n}): pick one from the list.",
      filter: "Filter by name…", openService: "Open the service", openTopic: "Open the flow", pick: "Click a box or a line to see what is behind it.",
      relPub: "publishes", relSub: "consumes", relHttp: "HTTP", relTrigger: "trigger", none: "Nothing to draw: no services linked to stores or topics in this graph.",
      noFlow: "No publisher or consumer of {q} in this graph.", moreData: "+{n} more not drawn",
      cardArch: "{s} services · {d} stores · {t} topics", cardQueues: "{n} sources", cardProblems: "{n} with a problem",
    },
    "pt-BR": {
      arch: "Arquitetura", archLead: "Como os serviços se ligam aos bancos e às filas: front-ends → APIs → tópicos → workers / functions → bancos. Espessura = número de acessos; tracejado = só ligações heurísticas. Clique numa caixa ou numa linha.",
      queues: "Filas e eventos", queuesLead: "Todo tópico, trigger do Firestore e job agendado: quem publica, quem consome e o que falta.",
      layers: ["Front-ends", "APIs / serviços", "Tópicos", "Workers / functions", "Bancos"],
      flowCols: ["O publicador lê", "Publica", "Evento", "Consumidores", "Grava"], next: "Próximo: evento", nextC: "Próximo: consumidores", nextW: "Próximo: grava",
      st_ok: "ok", st_no_consumer: "sem consumidor", st_no_publisher: "sem publicador",
      k_topic: "tópico", k_firestore: "trigger do Firestore", k_cron: "job agendado",
      w_no_consumer: "Nada nos repos escaneados consome {q}: o que é publicado aqui não é lido por nenhum serviço escaneado.",
      w_no_publisher: "Nada nos repos escaneados publica em {q}: os consumidores só rodam se algo fora destes repos enviar.",
      w_cross: "Liga serviços: {a} → {b}.", w_fields: "Lido pelo consumidor, mas nenhum publicador envia: {f}.",
      w_no_writer: "Nada nos repos escaneados grava {q}: o trigger só dispara com gravações de fora destes repos.",
      fields: "Mensagem", fPub: "Publicado", fRead: "Lido pelo consumidor", fMissing: "nunca publicado", fNone: "O scan não viu os campos da mensagem.",
      publishers: "Publicadores", writers: "Quem grava", consumers: "Consumidores", schedule: "Agenda", noOne: "nenhum nos repos escaneados", entry: "a partir de", writesW: "grava",
      source: "Origem", kind: "Tipo", status: "Status", all: "Todos", problems: "Com problema", topics: "Tópicos", triggers: "Triggers", jobs: "Jobs",
      accesses1: "1 acesso", accesses: "{n} acessos", calls1: "1 chamada", calls: "{n} chamadas", more: "+{n}",
      store: "{n} tabelas / coleções", expand: "Mostrar as tabelas no diagrama", collapse: "Esconder as tabelas", tooMany: "São muitas para desenhar ({n}): escolha uma na lista.",
      filter: "Filtrar por nome…", openService: "Abrir o serviço", openTopic: "Abrir o fluxo", pick: "Clique numa caixa ou numa linha para ver o que está por trás.",
      relPub: "publica", relSub: "consome", relHttp: "HTTP", relTrigger: "trigger", none: "Nada para desenhar: nenhum serviço ligado a bancos ou tópicos neste grafo.",
      noFlow: "Nenhum publicador ou consumidor de {q} neste grafo.", moreData: "+{n} não desenhados",
      cardArch: "{s} serviços · {d} bancos · {t} tópicos", cardQueues: "{n} origens", cardProblems: "{n} com problema",
    },
  };
  var V = VT[IX.lang] || VT.en;
  var VIEWS = HOME && HOME.views ? HOME.views : null;

  function viewLink(bi, kind) {
    if (bi === undefined || bi < 0) return null;
    if (kind === "entrypoint") return IX.routeDomain[String(bi)] !== undefined ? "#/r/" + bi : null;
    return kind === "function" ? "#/f/" + bi : null;
  }
  function itemHtml(it) {  // [label, service, book index, kind, evidence, (other end, rw)]
    var href = viewLink(it[2], it[3]);
    var name = href ? '<a href="' + href + '">' + esc(it[0]) + "</a>" : esc(it[0]);
    var tail = it.length > 5 && it[5] ? " → " + esc(it[5]) + (it[6] ? " <b>" + (it[6] === 3 ? H.RW : it[6] & 2 ? H.W : H.R) + "</b>" : "") : "";
    return "<li><span class=mono>" + name + "</span>" + tail + (it[4] ? ' <span class="ev">' + esc(it[4]) + "</span>" : "") + "</li>";
  }
  function statusChip(st) { return '<span class="st st-' + esc(st) + '">' + esc(V["st_" + st] || st) + "</span>"; }
  function evKindWord(k) { return V["k_" + k] || k; }
  function svcNames(list) { return list.map(function (s) { return sv(s); }).join(", "); }

  // ── one Cytoscape drawing with column headers (positions from the export) ──
  var VSTYLE = [
    { selector: "node", style: { "label": "data(label)", "shape": "round-rectangle", "width": 170, "height": 46, "text-wrap": "wrap", "text-max-width": 160,
      "text-valign": "center", "text-halign": "center", "font-size": 11, "background-color": "#eaeef2", "border-width": 1, "border-color": "#d0d7de", "color": "#1f2328" } },
    { selector: 'node[kind = "service"]', style: { "background-color": "#0969da", "border-color": "#0969da", "color": "#fff", "font-weight": 600 } },
    { selector: 'node[kind = "service"][role = "web"]', style: { "background-color": "#8250df", "border-color": "#8250df" } },
    { selector: 'node[kind = "service"][role = "worker"]', style: { "background-color": "#1a7f37", "border-color": "#1a7f37" } },
    { selector: 'node[kind = "store"]', style: { "shape": "barrel", "height": 58, "background-color": "#ddf4ff", "border-color": "#54aeff" } },
    { selector: 'node[kind = "topic"], node[kind = "event"]', style: { "shape": "cut-rectangle", "background-color": "#fff8c5", "border-color": "#d4a72c" } },
    { selector: 'node[warn = 1]', style: { "border-color": "#cf222e", "border-width": 2 } },
    { selector: 'node[kind = "data"]', style: { "shape": "barrel", "background-color": "#ddf4ff", "border-color": "#54aeff", "width": 160 } },
    { selector: 'node[kind = "fn"]', style: { "background-color": "#fff", "width": 180 } },
    { selector: 'node[kind = "ent"]', style: { "shape": "barrel", "height": 30, "width": 150, "font-size": 10, "background-color": "#f6f8fa", "border-color": "#54aeff" } },
    { selector: 'node[kind = "head"]', style: { "shape": "rectangle", "background-opacity": 0, "border-width": 0, "font-weight": 600, "color": "#656d76",
      "font-size": 12, "height": 20, "events": "no" } },
    { selector: "node:selected", style: { "border-width": 3, "border-color": "#cf222e" } },
    { selector: "edge", style: { "label": "data(label)", "curve-style": "bezier", "target-arrow-shape": "triangle", "width": "data(w)", "font-size": 10,
      "line-color": "#8c959f", "target-arrow-color": "#8c959f", "text-background-color": "#fff", "text-background-opacity": 1, "text-background-padding": 2 } },
    { selector: 'edge[rel = "pub"], edge[rel = "sub"]', style: { "line-color": "#d4a72c", "target-arrow-color": "#d4a72c" } },
    { selector: 'edge[rel = "http"]', style: { "line-color": "#8250df", "target-arrow-color": "#8250df" } },
    { selector: 'edge[rel = "trigger"]', style: { "line-color": "#1a7f37", "target-arrow-color": "#1a7f37" } },
    { selector: "edge[guess = 1]", style: { "line-style": "dashed" } },
    { selector: "edge[taxi = 1]", style: { "curve-style": "taxi", "taxi-direction": "horizontal", "taxi-turn": "data(turn)", "taxi-turn-min-distance": 10,
      "label": "", "source-label": "data(label)", "source-text-offset": 46, "text-background-opacity": 1 } },
    { selector: "edge[dim = 1]", style: { "opacity": 0.2 } },
    { selector: "edge:selected", style: { "line-color": "#cf222e", "target-arrow-color": "#cf222e" } },
  ];
  function headers(nodes, titleOf) {
    var cols = {}, top = Infinity;
    nodes.forEach(function (n) { cols[n.x] = n.layer; top = Math.min(top, n.y); });
    return Object.keys(cols).map(function (x) {
      return { data: { id: "h" + x, label: titleOf(cols[x]), kind: "head" }, position: { x: +x, y: (top === Infinity ? 0 : top) - 56 }, selectable: false, grabbable: false };
    });
  }
  function draw(box, els) {
    if (typeof cytoscape !== "function") return null;
    // an edge that skips columns runs as a right-angled line (taxi) and turns
    // just before its target column, so the long lines read as one bus
    var at = {};
    els.forEach(function (e) { if (e.position && e.data.kind !== "head") at[e.data.id] = e.position; });
    els.forEach(function (e) {
      var a = at[e.data.source], b = at[e.data.target];
      if (a && b && Math.abs(b.x - a.x) > 300) { e.data.taxi = 1; e.data.turn = Math.round(Math.abs(b.x - a.x) - 70); }
    });
    var ys = els.filter(function (e) { return e.position; }).map(function (e) { return e.position.y; });
    var xs = els.filter(function (e) { return e.position; }).map(function (e) { return e.position.x; });
    var spanY = ys.length ? Math.max.apply(null, ys) - Math.min.apply(null, ys) : 0;
    var spanX = xs.length ? Math.max.apply(null, xs) - Math.min.apply(null, xs) : 0;
    // tall enough for a readable zoom (the page scrolls; the wheel scrolls the page, not the drawing)
    var w = box.clientWidth || 1000, zoom = Math.min(1, (w - 60) / (spanX + 220));
    box.style.height = Math.max(220, Math.min(Math.round((spanY + 140) * zoom), 4000)) + "px";
    var cy = cytoscape({ container: box, elements: els, layout: { name: "preset", fit: true, padding: 30 }, minZoom: 0.15, maxZoom: 2.5,
      boxSelectionEnabled: false, autoungrabify: true, userZoomingEnabled: false, style: VSTYLE });
    if (cy.zoom() > 1) { cy.zoom(1); cy.center(); }  // a small flow keeps its real size
    var bar = document.createElement("div");
    bar.className = "vzoom";
    bar.innerHTML = '<button class="ctl" data-z="1.25" title="+">+</button><button class="ctl" data-z="0.8" title="−">−</button><button class="ctl" data-z="0" title="fit">⤢</button>';
    box.parentNode.insertBefore(bar, box);
    bar.onclick = function (ev) {
      var z = ev.target.closest("[data-z]");
      if (!z) return;
      var f = +z.getAttribute("data-z");
      if (!f) cy.fit(undefined, 30);
      else cy.zoom({ level: cy.zoom() * f, renderedPosition: { x: box.clientWidth / 2, y: box.clientHeight / 2 } });
    };
    return cy;
  }

  // ── #/architecture ──
  var ARCH = { open: {}, side: null };
  function pageArch() {
    wide(true); toc("arch");
    crumbs([esc(V.arch)]);
    if (!VIEWS || IX.chunks.indexOf("ar") < 0) return pageSystem();
    var A = chunk("ar");
    window.__bizmap.view = A;
    var h = '<div class="answer"><h1>' + esc(V.arch) + '</h1><p class="lead">' + esc(V.archLead) + "</p>";
    if (!A.nodes.length) { read(h + '<p class="muted">' + esc(V.none) + "</p></div>"); detail(""); return; }
    h += '<div class="vwrap"><div id="vmap" class="vmap"></div><div id="vside" class="vside"><p class="muted">' + esc(V.pick) + "</p></div></div></div>";  // side panel under the drawing
    read(h); detail("");
    var byId = {}, edgeById = {};
    A.nodes.forEach(function (n) { byId[n.id] = n; });
    A.edges.forEach(function (e) { edgeById[e.id] = e; });
    function nodeLabel(n) {
      if (n.kind === "store") return n.label + "\n" + Object.keys(n.count).map(function (k) { return n.count[k] + " " + (k === "collection" ? H.kindCollection : k === "view" ? H.kindView : H.kindTable) + (n.count[k] === 1 ? "" : "s"); }).join(" · ");
      if (n.kind === "topic") return n.label + (n.status !== "ok" ? "\n⚠ " + (V["st_" + n.status] || n.status) : "");
      return n.label;
    }
    function edgeLabel(e) {
      var rel = e.rel === "rw" ? (e.rw === 3 ? H.RW : e.rw & 2 ? H.W : H.R) : e.rel === "pub" ? V.relPub : e.rel === "sub" ? V.relSub : e.rel === "http" ? V.relHttp : V.relTrigger;
      return rel + " · " + e.n;
    }
    function elements() {
      var els = A.nodes.map(function (n) {
        return { data: { id: n.id, label: nodeLabel(n), kind: n.kind, role: n.role || "", warn: n.status && n.status !== "ok" ? 1 : 0 }, position: { x: n.x, y: n.y } };
      });
      var expanded = {};
      A.nodes.forEach(function (n) {
        if (n.kind !== "store" || !ARCH.open[n.id] || !n.ents.length) return;
        expanded[n.id] = 1;
        var step = 36, y0 = n.y - (n.ents.length - 1) * step / 2;
        n.ents.forEach(function (en, k) {
          var id = "x:" + en[0];
          els.push({ data: { id: id, label: en[1], kind: "ent", ref: en[0] }, position: { x: n.x + 230, y: y0 + k * step } });
          en[3].forEach(function (c) {
            if (!byId["s" + c[0]]) return;
            els.push({ data: { id: "s" + c[0] + ">" + id, source: "s" + c[0], target: id, label: c[1] === 3 ? H.RW : c[1] & 2 ? H.W : H.R, w: 1, guess: c[2], rel: "rw" } });
          });
        });
      });
      A.edges.forEach(function (e) {
        els.push({ data: { id: e.id, source: e.from, target: e.to, label: edgeLabel(e), w: e.w, guess: e.guess, rel: e.rel,
                           dim: expanded[e.to] || expanded[e.from] ? 1 : 0 } });
      });
      return els.concat(headers(A.nodes, function (layer) { return V.layers[layer] || ""; }));
    }
    var cy = draw($("vmap"), elements());
    window.__bizmap.viewNodes = A.nodes.map(function (n) { return n.id; });
    if (!cy) return;
    cy.on("tap", "node", function (ev) {
      var id = ev.target.id(), n = byId[id];
      if (ev.target.data("kind") === "ent") { location.hash = hrefOf(qPath(ev.target.data("ref"), "w")); return; }
      if (!n) return;
      if (n.kind === "topic") { location.hash = hrefOf("topic/" + encodeURIComponent(n.key)); return; }
      ARCH.side = n.id;
      $("vside").innerHTML = n.kind === "store" ? storeSide(n) : serviceSide(n);
      wireSide(n);
    });
    cy.on("tap", "edge", function (ev) {
      var e = edgeById[ev.target.id()];
      if (!e) return;
      var a = byId[e.from], b = byId[e.to];
      var count = e.rel === "rw" || e.rel === "trigger" ? (e.n === 1 ? V.accesses1 : fmt(V.accesses, { n: num(e.n) })) : (e.n === 1 ? V.calls1 : fmt(V.calls, { n: num(e.n) }));
      $("vside").innerHTML = "<h3>" + esc(a.label) + " → " + esc(b.label) + '</h3><p class="muted">' + esc(edgeLabel(e).split(" · ")[0]) + " · " + esc(count) +
        (e.guess ? " · " + esc(H.conf_heuristic) : "") + "</p><ul class=vlist>" + e.items.map(itemHtml).join("") + "</ul>" +
        (e.more ? '<p class="muted small">' + esc(fmt(V.more, { n: num(e.more) })) + "</p>" : "");
    });
    function serviceSide(n) {
      var lines = A.edges.filter(function (e) { return e.from === n.id || e.to === n.id; }).map(function (e) {
        var other = byId[e.from === n.id ? e.to : e.from];
        return "<li>" + (e.from === n.id ? "→ " : "← ") + esc(other.label) + " <b>" + esc(edgeLabel(e)) + "</b></li>";
      });
      return "<h3>" + esc(n.label) + ' <span class="kind">' + esc(n.role) + '</span></h3><p><a href="#/s/' + n.svc + '">' + esc(V.openService) + "</a></p><ul class=vlist>" + lines.join("") + "</ul>";
    }
    function storeSide(n) {
      var h = "<h3>" + esc(n.label) + '</h3><p class="muted">' + esc(fmt(V.store, { n: num(n.n) })) + "</p>";
      if (n.ents.length) h += '<p><button id="vexp" class="ctl">' + esc(ARCH.open[n.id] ? V.collapse : V.expand) + "</button></p>";
      else if (n.n) h += '<p class="muted small">' + esc(fmt(V.tooMany, { n: num(n.n) })) + "</p>";
      h += '<input id="vfil" class="ctl filter" placeholder="' + esc(V.filter) + '"><ul class="vlist" id="vents"></ul>';
      return h;
    }
    function wireSide(n) {
      if (n.kind !== "store") return;
      function list(q) {
        q = (q || "").toLowerCase();
        var rows = n.all.filter(function (e) { return !q || e[1].toLowerCase().indexOf(q) >= 0; });
        $("vents").innerHTML = rows.slice(0, 200).map(function (e) {
          return '<li><a href="' + hrefOf(qPath(e[0], "w")) + '">' + esc(e[1]) + '</a> <span class="kind">' + esc(e[2]) + "</span></li>";
        }).join("") + (rows.length > 200 ? '<li class="muted">' + esc(fmt(V.more, { n: num(rows.length - 200) })) + "</li>" : "");
      }
      list("");
      $("vfil").oninput = function () { list(this.value); };
      var b = $("vexp");
      if (b) b.onclick = function () { ARCH.open[n.id] = !ARCH.open[n.id]; pageArch(); };
    }
    if (ARCH.side && byId[ARCH.side]) {  // the panel stays open across a redraw (expand / collapse)
      var keep = byId[ARCH.side];
      $("vside").innerHTML = keep.kind === "store" ? storeSide(keep) : serviceSide(keep);
      wireSide(keep);
    }
  }

  // ── #/queues ──
  var QF = { tab: "all" };
  function pageQueues() {
    wide(true); toc("queues");
    crumbs([esc(V.queues)]);
    if (!VIEWS) return pageSystem();
    var rows = VIEWS.events;
    var tabs = [["all", V.all], ["problems", V.problems], ["topic", V.topics], ["firestore", V.triggers], ["cron", V.jobs]];
    var h = '<div class="answer"><h1>' + esc(V.queues) + '</h1><p class="lead">' + esc(V.queuesLead) + '</p><div class="tabs" id="qtabs">' + tabs.map(function (t) {
      var n = rows.filter(function (r) { return t[0] === "all" || (t[0] === "problems" ? r[4] !== "ok" : r[1] === t[0]); }).length;
      return '<a href="#" data-tab="' + t[0] + '" class="' + (QF.tab === t[0] ? "on" : "") + '">' + esc(t[1]) + " " + num(n) + "</a>";
    }).join("") + "</div>";
    var shown = rows.filter(function (r) { return QF.tab === "all" || (QF.tab === "problems" ? r[4] !== "ok" : r[1] === QF.tab); });
    h += '<table class="list" id="qtable"><thead><tr><th>' + esc(V.source) + "</th><th>" + esc(V.kind) + "</th><th>" + esc(V.publishers) + "</th><th>" + esc(V.consumers) +
         "</th><th>" + esc(V.status) + "</th></tr></thead><tbody>" + shown.map(function (r) {
      var pubs = r[1] === "cron" ? '<span class="muted">' + esc(r[3] || V.schedule) + "</span>" : r[5] ? esc(svcNames(r[7])) + ' <span class="muted">(' + r[5] + ")</span>" : '<span class="muted">—</span>';
      var cons = r[6] ? esc(svcNames(r[8])) + ' <span class="muted">(' + r[6] + ")</span>" : '<span class="muted">—</span>';
      var extra = (r[9] || []).indexOf("unpublished_fields") >= 0 ? ' <span class="risk small" title="' + esc(V.fMissing) + '">⚠ ' + esc(V.fields) + "</span>" : "";
      return '<tr class="row" data-href="' + hrefOf("topic/" + encodeURIComponent(r[0])) + '"><td class="mono"><b>' + esc(r[2]) + "</b></td><td>" + esc(evKindWord(r[1])) +
             (r[1] !== "cron" && r[3] ? ' <span class="muted small">' + esc(r[3]) + "</span>" : "") + "</td><td>" + pubs + "</td><td>" + cons + "</td><td>" + statusChip(r[4]) + extra + "</td></tr>";
    }).join("") + "</tbody></table></div>";
    read(h); detail("");
    Array.prototype.forEach.call(document.querySelectorAll("#qtabs a"), function (a) {
      a.onclick = function (ev) { ev.preventDefault(); QF.tab = a.getAttribute("data-tab"); pageQueues(); };
    });
  }

  // ── #/topic/<key>: one event source, end to end ──
  function findFlow(key) {
    var flows = chunk("ev").flows;
    for (var i = 0; i < flows.length; i++) if (flows[i].key === key || flows[i].node === key) return flows[i];
    return null;
  }
  function pageEvent(key) {
    if (!VIEWS || IX.chunks.indexOf("ev") < 0) return pageTopic(key);
    var F = findFlow(key);
    if (!F) {
      if (engine().has(key)) return pageTopic(key);
      wide(true); toc("queues"); crumbs(['<a href="' + hrefOf("queues") + '">' + esc(V.queues) + "</a>", esc(key)]);
      read('<div class="answer"><h1>' + esc(key) + '</h1><p class="muted">' + esc(fmt(V.noFlow, { q: key })) + "</p></div>"); detail("");
      return;
    }
    wide(true); toc("queues");
    crumbs(['<a href="' + hrefOf("queues") + '">' + esc(V.queues) + "</a>", esc(F.label)]);
    window.__bizmap.view = F;
    var q = F.label, warn = [];  // [text, info only]
    if (F.warnings.indexOf("no_consumer") >= 0) warn.push([fmt(V.w_no_consumer, { q: q }), 0]);
    if (F.warnings.indexOf("no_publisher") >= 0) warn.push([fmt(F.kind === "firestore" ? V.w_no_writer : V.w_no_publisher, { q: q }), 0]);
    if (F.fields && F.fields.missing.length) warn.push([fmt(V.w_fields, { f: F.fields.missing.join(", ") }), 0]);
    if (F.warnings.indexOf("cross_service") >= 0) warn.push([fmt(V.w_cross, { a: svcNames(F.pubSvcs), b: svcNames(F.conSvcs) }), 1]);
    var h = '<div class="answer"><h1><span class="topic">' + esc(q) + '</span> <span class="kind">' + esc(evKindWord(F.kind) + (F.detail ? " · " + F.detail : "")) + "</span> " +
            statusChip(F.status) + "</h1>";
    if (warn.length) h += '<div class="vwarn" id="vwarn">' + warn.map(function (w) {
      return '<p class="' + (w[1] ? "info" : "bad") + '">' + (w[1] ? "↔ " : "⚠ ") + esc(w[0]) + "</p>";
    }).join("") + "</div>";
    h += '<div id="vmap" class="vmap"></div>';
    var more = Object.keys(F.more || {}).filter(function (k) { return F.more[k]; });
    if (more.length) h += '<p class="muted small">' + more.map(function (k) { return esc(fmt(V.moreData, { n: num(F.more[k]) })); }).join(" · ") + "</p>";
    // message shape
    if (F.kind !== "cron") h += "<h3>" + esc(V.fields) + "</h3>";
    if (F.kind === "cron") { /* a schedule carries no message */ }
    else if (F.fields) {
      var miss = {};
      F.fields.missing.forEach(function (f) { miss[f] = 1; });
      h += '<table class="list vfields"><tbody><tr><th>' + esc(V.fPub) + "</th><td>" + (F.fields.pub.map(function (f) { return "<code>" + esc(f) + "</code>"; }).join(" ") || "—") +
           "</td></tr><tr><th>" + esc(V.fRead) + "</th><td>" + (F.fields.read.map(function (f) {
             return miss[f] ? '<code class="miss" title="' + esc(V.fMissing) + '">' + esc(f) + "</code> <span class=risk>⚠ " + esc(V.fMissing) + "</span>" : "<code>" + esc(f) + "</code>";
           }).join(" ") || "—") + "</td></tr></tbody></table>";
    } else h += '<p class="muted">' + esc(V.fNone) + "</p>";
    // publishers and consumers as text
    h += "<h3>" + esc(F.kind === "firestore" ? V.writers : F.kind === "cron" ? V.schedule : V.publishers) + "</h3>";
    if (F.kind === "cron") h += "<p>" + esc(F.detail || "—") + "</p>";
    else h += F.publishers.length ? "<ul>" + F.publishers.map(function (p) {
      var li = itemHtml(p.fn).replace(/^<li>|<\/li>$/g, "");
      return "<li>" + li + ' <span class="muted">[' + esc(sv(p.fn[1])) + "]</span>" + (p.entry.length ? ' <span class="muted">' + esc(V.entry) + " " + esc(p.entry.join(", ")) + "</span>" : "") + "</li>";
    }).join("") + "</ul>" : '<p class="muted">' + esc(V.noOne) + "</p>";
    h += "<h3>" + esc(V.consumers) + "</h3>" + (F.consumers.length ? "<ul>" + F.consumers.map(function (c) {
      var li = itemHtml(c.fn).replace(/^<li>|<\/li>$/g, "");
      return "<li>" + li + ' <span class="muted">[' + esc(sv(c.fn[1])) + "]</span>" + (c.writes.length ? "; " + esc(V.writesW) + " " + c.writes.map(function (r) {
        return '<a href="' + hrefOf(qPath(r, "w")) + '">' + esc(entName(r)) + "</a>";
      }).join(", ") : "") + "</li>";
    }).join("") + "</ul>" : '<p class="muted">' + esc(V.noOne) + "</p>");
    read(h + "</div>"); detail("");
    var els = F.nodes.map(function (n) {
      var warnNode = n.kind === "event" && n.key === F.key && F.status !== "ok";
      return { data: { id: n.id, label: n.label + (n.sub ? "\n" + n.sub : ""), kind: n.kind, warn: warnNode ? 1 : 0 }, position: { x: n.x, y: n.y } };
    });
    F.edges.forEach(function (e) {
      var lab = e.rel === "reads" ? H.R : e.rel === "writes" ? H.W : e.rel === "trigger" ? V.relTrigger : "";
      els.push({ data: { id: e.id, source: e.from, target: e.to, label: lab, w: 1.6, guess: e.guess, rel: e.rel } });
    });
    els = els.concat(headers(F.nodes, function (layer) {
      if (layer < 5) return V.flowCols[layer];
      return [V.next, V.nextC, V.nextW][(layer - 5) % 3];
    }));
    var byId = {};
    F.nodes.forEach(function (n) { byId[n.id] = n; });
    window.__bizmap.viewNodes = F.nodes.map(function (n) { return n.id; });
    var cy = draw($("vmap"), els);
    if (!cy) return;
    cy.on("tap", "node", function (ev) {
      var n = byId[ev.target.id()];
      if (!n) return;
      if (n.kind === "data") location.hash = hrefOf(qPath(n.ref, "w"));
      else if (n.kind === "event" && n.key !== F.key) location.hash = hrefOf("topic/" + encodeURIComponent(n.key));
      else if (n.kind === "fn") { var href = viewLink(n.link, n.fk); if (href) location.hash = href; }
    });
  }

  // ── start screen: two cards that open the views ──
  function viewCards() {
    if (!VIEWS) return "";
    var ev = VIEWS.events;
    return '<div class="finds vcards" id="vcards"><div class="fcard view" data-card="arch" data-href="' + esc(hrefOf("architecture")) + '"><h3>' + esc(V.arch) +
      ' <span class="go">→</span></h3><p class="muted small">' + esc(fmt(V.cardArch, { s: num(VIEWS.services), d: num(VIEWS.stores), t: num(VIEWS.topics) })) + "</p></div>" +
      '<div class="fcard view" data-card="queues" data-href="' + esc(hrefOf("queues")) + '"><h3>' + esc(V.queues) + ' <span class="go">→</span></h3><p class="muted small">' +
      esc(fmt(V.cardQueues, { n: num(ev.length) })) + (VIEWS.problems ? ' · <span class="risk">' + esc(fmt(V.cardProblems, { n: num(VIEWS.problems) })) + "</span>" : "") + "</p></div></div>";
  }

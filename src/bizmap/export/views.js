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
      st_ok: "ok", st_no_consumer: "no consumer in the scanned repos", st_no_publisher: "no publisher in the scanned repos",
      box_no_consumer: "no consumer", box_no_publisher: "no publisher",
      k_topic: "topic", k_firestore: "Firestore trigger", k_cron: "scheduled job",
      w_no_consumer: "Nothing in the scanned repos consumes {q}: messages published here are not read by any scanned service (a consumer may live in a repo outside this scan).",
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
      fAll: "All", fData: "Data", fHttp: "HTTP", fEvents: "Queues and triggers", hoverHint: "Hover a box to light its lines; click a line for what is behind it.",
      topicGroup: "{s} · {n} topics", topicGroupWarn: "{n} without a consumer", clickOpen: "click to open", unfoldTopics: "show its topics one by one", foldTopics: "fold its topics",
    },
    "pt-BR": {
      arch: "Arquitetura", archLead: "Como os serviços se ligam aos bancos e às filas: front-ends → APIs → tópicos → workers / functions → bancos. Espessura = número de acessos; tracejado = só ligações heurísticas. Clique numa caixa ou numa linha.",
      queues: "Filas e eventos", queuesLead: "Todo tópico, trigger do Firestore e job agendado: quem publica, quem consome e o que falta.",
      layers: ["Front-ends", "APIs / serviços", "Tópicos", "Workers / functions", "Bancos"],
      flowCols: ["O publicador lê", "Publica", "Evento", "Consumidores", "Grava"], next: "Próximo: evento", nextC: "Próximo: consumidores", nextW: "Próximo: grava",
      st_ok: "ok", st_no_consumer: "nenhum consumidor nos repos escaneados", st_no_publisher: "nenhum publicador nos repos escaneados",
      box_no_consumer: "sem consumidor", box_no_publisher: "sem publicador",
      k_topic: "tópico", k_firestore: "trigger do Firestore", k_cron: "job agendado",
      w_no_consumer: "Nada nos repos escaneados consome {q}: o que é publicado aqui não é lido por nenhum serviço escaneado (o consumidor pode estar num repo fora deste scan).",
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
      fAll: "Tudo", fData: "Dados", fHttp: "HTTP", fEvents: "Filas e triggers", hoverHint: "Passe o mouse numa caixa para destacar as linhas dela; clique numa linha para ver o que está por trás.",
      topicGroup: "{s} · {n} tópicos", topicGroupWarn: "{n} sem consumidor", clickOpen: "clique para abrir", unfoldTopics: "mostrar os tópicos um a um", foldTopics: "agrupar os tópicos",
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

  // ── one Mermaid drawing (the renderer GitHub uses for the PR comment) ──
  // def: { groups: [{id, label, cls, nodes: [ids], parent}], nodes: [{id, label, sub, shape, cls, title}],
  //        edges: [{from, to, label, dashed, width, color, back}], chain: [group ids, invisible order] }
  // Node / edge ids are ours; Mermaid gets short ones (n0, g0) so any name is safe.
  var MM = { ready: false, seq: 0 };
  function mmInit() {
    if (MM.ready || typeof mermaid === "undefined") return MM.ready;
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict", maxTextSize: 5000000, maxEdges: 20000, theme: "base",
      themeVariables: { fontFamily: getComputedStyle(document.body).fontFamily, fontSize: "13px", primaryColor: "#f6f8fa", primaryBorderColor: "#d0d7de",
                        lineColor: "#8c959f", clusterBkg: "#f6f8fa", clusterBorder: "#d0d7de", edgeLabelBackground: "#ffffff" },
      flowchart: { htmlLabels: true, curve: "basis", nodeSpacing: 26, rankSpacing: 64, padding: 10, useMaxWidth: false, diagramPadding: 12 } });
    MM.ready = true;
    return true;
  }
  function mmText(s) {  // a label Mermaid shows as text, never markup
    return String(s).replace(/&/g, "#amp;").replace(/"/g, "#quot;").replace(/</g, "#lt;").replace(/>/g, "#gt;");
  }
  var MM_CLASS = [
    "classDef web fill:#8250df,stroke:#8250df,color:#fff,font-weight:600",
    "classDef api fill:#0969da,stroke:#0969da,color:#fff,font-weight:600",
    "classDef lib fill:#57606a,stroke:#57606a,color:#fff,font-weight:600",
    "classDef worker fill:#1a7f37,stroke:#1a7f37,color:#fff,font-weight:600",
    "classDef store fill:#ddf4ff,stroke:#54aeff,color:#1f2328",
    "classDef topic fill:#fff8c5,stroke:#d4a72c,color:#1f2328",
    "classDef fn fill:#ffffff,stroke:#d0d7de,color:#1f2328",
    "classDef col fill:#f6f8fa,stroke:#d0d7de,color:#57606a",
    "classDef more fill:#f6f8fa,stroke:#adb5bd,color:#57606a,stroke-dasharray:3 3",
    "classDef warn stroke:#cf222e,stroke-width:2px",
    "classDef center stroke:#cf222e,stroke-width:3px",
  ];
  function mmBuild(def) {
    var ids = {}, gids = {}, lines = ["flowchart LR"], inGroup = {};
    def.nodes.forEach(function (n, k) { ids[n.id] = "n" + k; });
    (def.groups || []).forEach(function (g, k) { gids[g.id] = "g" + k; g.nodes.forEach(function (id) { inGroup[id] = g.id; }); });
    function nodeLine(n) {
      var t = mmText(n.label) + (n.sub ? "<br/><small>" + mmText(n.sub) + "</small>" : "");
      var m = ids[n.id];
      if (n.shape === "db") return m + '[("' + t + '")]';
      if (n.shape === "topic") return m + '(["' + t + '"])';
      if (n.shape === "round") return m + '("' + t + '")';
      return m + '["' + t + '"]';
    }
    var byGroup = {};
    def.nodes.forEach(function (n) { (byGroup[inGroup[n.id] || ""] = byGroup[inGroup[n.id] || ""] || []).push(n); });
    function emitGroup(g, pad) {
      lines.push(pad + "subgraph " + gids[g.id] + '["' + mmText(g.label) + '"]');
      (def.groups || []).forEach(function (c) { if (c.parent === g.id) emitGroup(c, pad + "  "); });
      (byGroup[g.id] || []).forEach(function (n) { lines.push(pad + "  " + nodeLine(n)); });
      lines.push(pad + "end");
    }
    (def.groups || []).forEach(function (g) { if (!g.parent) emitGroup(g, "  "); });
    (byGroup[""] || []).forEach(function (n) { lines.push("  " + nodeLine(n)); });
    function ref(id) { return ids[id] || gids[id]; }
    var seen = {}, edgeKeys = [];
    def.edges.forEach(function (e) {
      var a = ref(e.from), b = ref(e.to);
      if (!a || !b) { edgeKeys.push(null); return; }
      // Mermaid ranks by link direction: a link that flows right to left is written left to right with its head on the left
      var l = e.back ? b : a, r = e.back ? a : b;
      var arrow = e.back ? (e.dashed ? "<-.-" : "<---") : (e.dashed ? "-.->" : "-->");
      if (e.plain) arrow = e.dashed ? "-.-" : "---";
      lines.push("  " + l + " " + arrow + (e.label ? '|"' + mmText(e.label) + '"|' : "") + " " + r);
      var pair = l + "_" + r;
      seen[pair] = (seen[pair] || 0);
      edgeKeys.push("L_" + pair + "_" + seen[pair]);
      seen[pair]++;
    });
    def.edges.forEach(function (e, k) {
      if (!edgeKeys[k]) return;
      var st = [];
      if (e.color) st.push("stroke:" + e.color);
      if (e.width) st.push("stroke-width:" + e.width + "px");
      if (st.length) lines.push("  linkStyle " + edgeKeys.filter(function (x, j) { return j < k && x; }).length + " " + st.join(","));
    });
    var chain = (def.chain || []).map(ref).filter(Boolean);
    if (chain.length > 1) lines.push("  " + chain.join(" ~~~ "));
    lines = lines.concat(MM_CLASS.map(function (c) { return "  " + c; }));
    var byCls = {};
    def.nodes.forEach(function (n) { String(n.cls || "").split(" ").filter(Boolean).forEach(function (c) { (byCls[c] = byCls[c] || []).push(ids[n.id]); }); });
    Object.keys(byCls).sort().forEach(function (c) { lines.push("  class " + byCls[c].join(",") + " " + c); });
    return { text: lines.join("\n"), ids: ids, gids: gids, edgeKeys: edgeKeys };
  }
  // draws def in box; on.node(id), on.group(id), on.edge(index) on click; hover a box to light its lines
  function mmDraw(box, def, on) {
    on = on || {};
    var built = mmBuild(def);
    window.__bizmap.mermaid = built.text;
    if (!mmInit()) { box.innerHTML = '<pre class="mono small">' + esc(built.text) + "</pre>"; return Promise.resolve(null); }
    var diag = "mm" + (++MM.seq);
    box.innerHTML = '<p class="muted small">…</p>';
    return mermaid.render(diag, built.text).then(function (res) {
      if (!box.isConnected) return null;
      box.innerHTML = res.svg;
      var svg = box.querySelector("svg");
      var mid2id = {}, gid2id = {};
      Object.keys(built.ids).forEach(function (k) { mid2id[built.ids[k]] = k; });
      Object.keys(built.gids).forEach(function (k) { gid2id[built.gids[k]] = k; });
      var title = {};
      def.nodes.forEach(function (n) { if (n.title) title[n.id] = n.title; });
      // node -> its edges, edge -> its DOM parts
      var adj = {}, parts = [];
      def.edges.forEach(function (e, k) {
        (adj[e.from] = adj[e.from] || []).push(k);
        (adj[e.to] = adj[e.to] || []).push(k);
        var key = built.edgeKeys[k];
        parts[k] = key ? Array.prototype.slice.call(svg.querySelectorAll('[data-id="' + key + '"]')).map(function (el) {
          return el.closest("g.edgeLabel") || el;
        }) : [];
      });
      var nodeEls = {};
      Array.prototype.forEach.call(svg.querySelectorAll("g.node"), function (g) {
        var m = /-flowchart-(n\d+)-\d+$/.exec(g.id);
        if (!m || !mid2id[m[1]]) return;
        var id = mid2id[m[1]];
        nodeEls[id] = g;
        g.setAttribute("data-node", id);
        g.style.cursor = "pointer";
        if (title[id]) { var t = document.createElementNS("http://www.w3.org/2000/svg", "title"); t.textContent = title[id]; g.appendChild(t); }
      });
      function light(id) {
        svg.classList.toggle("hl", !!id);
        Array.prototype.forEach.call(svg.querySelectorAll(".on"), function (el) { el.classList.remove("on"); });
        if (!id) return;
        if (nodeEls[id]) nodeEls[id].classList.add("on");
        (adj[id] || []).forEach(function (k) {
          parts[k].forEach(function (el) { el.classList.add("on"); });
          [def.edges[k].from, def.edges[k].to].forEach(function (x) { if (nodeEls[x]) nodeEls[x].classList.add("on"); });
        });
      }
      var pinned = null;
      Object.keys(nodeEls).forEach(function (id) {
        var g = nodeEls[id];
        g.addEventListener("mouseenter", function () { if (!pinned) light(id); });
        g.addEventListener("mouseleave", function () { if (!pinned) light(null); });
        g.addEventListener("click", function () { pinned = id; light(id); if (on.node) on.node(id); });
      });
      Array.prototype.forEach.call(svg.querySelectorAll("g.cluster"), function (g) {
        var gid = g.id.slice(diag.length + 1);
        if (!gid2id[gid] || !on.group) return;
        var lab = g.querySelector(".cluster-label");
        if (lab) { lab.style.cursor = "pointer"; lab.addEventListener("click", function () { on.group(gid2id[gid]); }); }
      });
      parts.forEach(function (els, k) {
        els.forEach(function (el) {
          el.style.cursor = "pointer";
          el.addEventListener("click", function (ev) {
            ev.stopPropagation();
            pinned = null; light(null);
            svg.classList.add("hl");
            els.forEach(function (x) { x.classList.add("on"); });
            [def.edges[k].from, def.edges[k].to].forEach(function (x) { if (nodeEls[x]) nodeEls[x].classList.add("on"); });
            if (on.edge) on.edge(k);
          });
        });
      });
      svg.addEventListener("click", function (ev) { if (ev.target === svg) { pinned = null; light(null); } });
      // size: the natural width, shrunk to the box (not below 55%); + / − / fit buttons
      var natural = parseFloat(svg.getAttribute("width")) || svg.getBBox().width;
      var minScale = on.minScale || 0.55;  // narrower than this: the box scrolls sideways
      var scale = Math.min(1, Math.max(minScale, (box.clientWidth - 16) / natural));
      function zoom(z) {
        scale = z;
        svg.style.width = Math.round(natural * scale) + "px";
        svg.style.height = "auto";
        svg.style.maxWidth = "none";
      }
      zoom(scale);
      var bar = document.createElement("div");
      bar.className = "vzoom";
      bar.innerHTML = '<button class="ctl" data-z="1.25" title="+">+</button><button class="ctl" data-z="0.8" title="−">−</button><button class="ctl" data-z="0" title="fit">⤢</button>';
      if (box.previousElementSibling && box.previousElementSibling.className === "vzoom") box.parentNode.removeChild(box.previousElementSibling);
      box.parentNode.insertBefore(bar, box);
      bar.onclick = function (ev) {
        var z = ev.target.closest("[data-z]");
        if (!z) return;
        var f = +z.getAttribute("data-z");
        zoom(f ? Math.max(0.2, Math.min(3, scale * f)) : Math.min(1, (box.clientWidth - 16) / natural));
      };
      return { svg: svg, light: light, built: built };
    }).catch(function (err) {
      if (box.isConnected) box.innerHTML = '<p class="risk">' + esc(String(err && err.message || err)) + '</p><pre class="mono small">' + esc(built.text) + "</pre>";
      return null;
    });
  }
  var REL_COLOR = { rw: "#8c959f", pub: "#d4a72c", sub: "#d4a72c", http: "#8250df", trigger: "#1a7f37", reads: "#8c959f", writes: "#8c959f", own: "#d0d7de" };

  // ── #/architecture ──
  var ARCH = { open: {}, side: null, layer: null };
  var ARCH_BIG = 45;  // more lines than this: open on one layer at a time
  var REL_GROUP = { rw: "rw", http: "http", pub: "ev", sub: "ev", trigger: "ev" };
  function pageArch() {
    wide(true); toc("arch");
    crumbs([esc(V.arch)]);
    if (!VIEWS || IX.chunks.indexOf("ar") < 0) return pageSystem();
    var A = chunk("ar");
    window.__bizmap.view = A;
    var h = '<div class="answer"><h1>' + esc(V.arch) + '</h1><p class="lead">' + esc(V.archLead) + "</p>";
    if (!A.nodes.length) { read(h + '<p class="muted">' + esc(V.none) + "</p></div>"); detail(""); return; }
    // one layer at a time (or all): a big system reads layer by layer
    var counts = { rw: 0, http: 0, ev: 0 };
    A.edges.forEach(function (e) { counts[REL_GROUP[e.rel] || "rw"]++; });
    if (!ARCH.layer) ARCH.layer = A.edges.length > ARCH_BIG ? (counts.rw ? "rw" : counts.http ? "http" : "ev") : "all";
    h += '<div class="bar"><div class="tabs" id="vlayers">' + [["all", V.fAll, A.edges.length], ["rw", V.fData, counts.rw], ["http", V.fHttp, counts.http], ["ev", V.fEvents, counts.ev]].map(function (f) {
      return '<a href="#" data-l="' + f[0] + '" class="' + (ARCH.layer === f[0] ? "on" : "") + '">' + esc(f[1]) + " " + num(f[2]) + "</a>";
    }).join("") + '</div><span class="muted small">' + esc(V.hoverHint) + "</span></div>";
    h += '<div class="vwrap"><div id="vmap" class="vmap mm"></div><div id="vside" class="vside"><p class="muted">' + esc(V.pick) + "</p></div></div></div>";
    read(h); detail("");
    Array.prototype.forEach.call(document.querySelectorAll("#vlayers a"), function (a) {
      a.onclick = function (ev) { ev.preventDefault(); ARCH.layer = a.getAttribute("data-l"); pageArch(); };
    });
    var byId = {}, edgeById = {};
    A.nodes.forEach(function (n) { byId[n.id] = n; });
    A.edges.forEach(function (e) { edgeById[e.id] = e; });
    // topics only one service publishes to, three or more: one box per publisher until opened
    var pubsOf = {}, topicsOf = {};
    A.edges.forEach(function (e) { if (e.rel === "pub") (pubsOf[e.to] = pubsOf[e.to] || {})[e.from] = 1; });
    Object.keys(pubsOf).forEach(function (t) { var ps = Object.keys(pubsOf[t]); if (ps.length === 1) (topicsOf[ps[0]] = topicsOf[ps[0]] || []).push(t); });
    var groupOf = {};
    Object.keys(topicsOf).forEach(function (sid) { if (topicsOf[sid].length >= 3 && !ARCH.open[sid]) topicsOf[sid].forEach(function (t) { groupOf[t] = "tg:" + sid; }); });
    function edgeLabel(e) {
      var rel = e.rel === "rw" ? (e.rw === 3 ? H.RW : e.rw & 2 ? H.W : H.R) : e.rel === "pub" ? V.relPub : e.rel === "sub" ? V.relSub : e.rel === "http" ? V.relHttp : V.relTrigger;
      return e.n > 1 || e.rel === "rw" || e.rel === "http" ? rel + " · " + num(e.n) : rel;
    }
    // the edges drawn: filtered, with grouped topics folded into their group box
    var drawn = [], merged = {};
    A.edges.forEach(function (e) {
      if (ARCH.layer !== "all" && ARCH.layer !== (REL_GROUP[e.rel] || "rw")) return;
      var a = groupOf[e.from] || e.from, b = groupOf[e.to] || e.to;
      var key = a + ">" + b + ">" + e.rel;
      if (merged[key] !== undefined) {
        var m = drawn[merged[key]];
        m.n += e.n; m.src.push(e); m.guess = m.guess && e.guess; m.w = Math.max(m.w, e.w);
        return;
      }
      merged[key] = drawn.length;
      drawn.push({ from: a, to: b, rel: e.rel, rw: e.rw, n: e.n, guess: e.guess, w: e.w, src: [e] });
    });
    var used = {};
    drawn.forEach(function (d) { used[d.from] = 1; used[d.to] = 1; });
    var layerOf = {}, groups = V.layers.map(function (title, k) { return { id: "L" + k, label: title, nodes: [] }; });
    var nodes = [];
    function addNode(n) {
      if (!used[n.id]) return;
      layerOf[n.id] = n.layer;
      groups[n.layer].nodes.push(n.id);
      nodes.push(n);
    }
    A.nodes.slice().sort(function (p, q) { return p.layer - q.layer || p.y - q.y || (p.id < q.id ? -1 : 1); }).forEach(function (n) {
      if (groupOf[n.id]) return;
      if (n.kind === "store") {
        var count = Object.keys(n.count).map(function (k) { return num(n.count[k]) + " " + (k === "collection" ? H.kindCollection : k === "view" ? H.kindView : H.kindTable) + (n.count[k] === 1 ? "" : "s"); }).join(" · ");
        addNode({ id: n.id, layer: 4, label: n.label, sub: count, shape: "db", cls: "store" });
      } else if (n.kind === "topic") {
        addNode({ id: n.id, layer: 2, label: n.label, sub: n.status !== "ok" ? "⚠ " + (V["box_" + n.status] || n.status) : "", shape: "topic",
                  cls: "topic" + (n.status !== "ok" ? " warn" : "") });
      } else {
        addNode({ id: n.id, layer: n.layer, label: n.label, cls: n.role === "lib" ? "lib" : n.role });
      }
    });
    Object.keys(topicsOf).forEach(function (sid) {
      var gid = "tg:" + sid;
      if (!used[gid] || !topicsOf[sid].length || ARCH.open[sid]) return;
      var warn = topicsOf[sid].filter(function (t) { return byId[t] && byId[t].status !== "ok"; }).length;
      addNode({ id: gid, layer: 2, label: fmt(V.topicGroup, { s: byId[sid].label, n: num(topicsOf[sid].length) }),
                sub: (warn ? "⚠ " + fmt(V.topicGroupWarn, { n: num(warn) }) + " · " : "") + V.clickOpen, shape: "topic", cls: "topic" + (warn ? " warn" : "") });
    });
    var top = 1;
    drawn.forEach(function (d) { top = Math.max(top, d.n); });
    var def = { groups: groups.filter(function (g) { return g.nodes.length; }), nodes: nodes,
      edges: drawn.map(function (d) {
        return { from: d.from, to: d.to, label: edgeLabel(d), dashed: !!d.guess, color: REL_COLOR[d.rel],
                 width: Math.round((1.2 + 4.3 * Math.log(1 + d.n) / Math.log(1 + top)) * 10) / 10, back: layerOf[d.from] > layerOf[d.to] };
      }) };
    def.chain = def.groups.map(function (g) { return g.id; });
    window.__bizmap.viewNodes = nodes.map(function (n) { return n.id; });
    function showEdge(k) {
      var d = drawn[k], a = byId[d.from], b = byId[d.to];
      var la = a ? a.label : nodes.filter(function (n) { return n.id === d.from; })[0].label, lb = b ? b.label : nodes.filter(function (n) { return n.id === d.to; })[0].label;
      var items = [], more = 0;
      d.src.forEach(function (e) { items = items.concat(e.items); more += e.more; });
      var count = d.rel === "rw" || d.rel === "trigger" ? (d.n === 1 ? V.accesses1 : fmt(V.accesses, { n: num(d.n) })) : (d.n === 1 ? V.calls1 : fmt(V.calls, { n: num(d.n) }));
      $("vside").innerHTML = "<h3>" + esc(la) + " → " + esc(lb) + '</h3><p class="muted">' + esc(edgeLabel(d).split(" · ")[0]) + " · " + esc(count) +
        (d.guess ? " · " + esc(H.conf_heuristic) : "") + "</p><ul class=vlist>" + items.slice(0, 60).map(itemHtml).join("") + "</ul>" +
        (more || items.length > 60 ? '<p class="muted small">' + esc(fmt(V.more, { n: num(more + Math.max(0, items.length - 60)) })) + "</p>" : "");
    }
    function serviceSide(n) {
      var lines = A.edges.filter(function (e) { return e.from === n.id || e.to === n.id; }).map(function (e) {
        var other = byId[e.from === n.id ? e.to : e.from];
        return "<li>" + (e.from === n.id ? "→ " : "← ") + esc(other.label) + " <b>" + esc(edgeLabel(e)) + "</b></li>";
      });
      return "<h3>" + esc(n.label) + ' <span class="kind">' + esc(n.role) + '</span></h3><p><a href="#/s/' + n.svc + '">' + esc(V.openService) + "</a>" +
        (topicsOf[n.id] && topicsOf[n.id].length >= 3 ? ' · <a href="#" id="vtg">' + esc(ARCH.open[n.id] ? V.foldTopics : V.unfoldTopics) + "</a>" : "") +
        "</p><ul class=vlist>" + lines.join("") + "</ul>";
    }
    function storeSide(n) {
      return "<h3>" + esc(n.label) + '</h3><p class="muted">' + esc(fmt(V.store, { n: num(n.n) })) + "</p>" +
        '<input id="vfil" class="ctl filter" placeholder="' + esc(V.filter) + '"><ul class="vlist" id="vents"></ul>';
    }
    function side(id) {
      var n = byId[id];
      if (!n) return;
      ARCH.side = id;
      $("vside").innerHTML = n.kind === "store" ? storeSide(n) : serviceSide(n);
      if (n.kind === "store") {
        var list = function (q) {
          q = (q || "").toLowerCase();
          var rows = n.all.filter(function (e) { return !q || e[1].toLowerCase().indexOf(q) >= 0; });
          $("vents").innerHTML = rows.slice(0, 200).map(function (e) {
            return '<li><a href="' + hrefOf(qPath(e[0], "w")) + '">' + esc(e[1]) + '</a> <span class="kind">' + esc(e[2]) + "</span></li>";
          }).join("") + (rows.length > 200 ? '<li class="muted">' + esc(fmt(V.more, { n: num(rows.length - 200) })) + "</li>" : "");
        };
        list("");
        $("vfil").oninput = function () { list(this.value); };
      }
      var tg = $("vtg");
      if (tg) tg.onclick = function (ev) { ev.preventDefault(); ARCH.open[id] = !ARCH.open[id]; pageArch(); };
    }
    mmDraw($("vmap"), def, {
      node: function (id) {
        if (id.indexOf("tg:") === 0) { ARCH.open[id.slice(3)] = 1; pageArch(); return; }
        var n = byId[id];
        if (n && n.kind === "topic") { location.hash = hrefOf("topic/" + encodeURIComponent(n.key)); return; }
        side(id);
      },
      edge: showEdge,
      group: function () {},
    }).then(function () { if (ARCH.side && byId[ARCH.side] && $("vside")) side(ARCH.side); });
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
    var cols = {};
    F.nodes.forEach(function (n) { (cols[n.layer] = cols[n.layer] || []).push(n); });
    var layers = Object.keys(cols).map(Number).sort(function (a, b) { return a - b; });
    function colTitle(layer) { return layer < 5 ? V.flowCols[layer] : [V.next, V.nextC, V.nextW][(layer - 5) % 3]; }
    var def = {
      groups: layers.map(function (l) { return { id: "C" + l, label: colTitle(l), nodes: cols[l].slice().sort(function (p, q) { return p.y - q.y; }).map(function (n) { return n.id; }) }; }),
      nodes: F.nodes.map(function (n) {
        var warnNode = n.kind === "event" && n.key === F.key && F.status !== "ok";
        return { id: n.id, label: n.label, sub: n.sub || "", shape: n.kind === "data" ? "db" : n.kind === "event" ? "topic" : "box",
                 cls: n.kind === "data" ? "store" : n.kind === "event" ? "topic" + (warnNode ? " warn" : "") : n.svc >= 0 && SV[n.svc] ? (SV[n.svc].kind === "lib" ? "lib" : SV[n.svc].kind) : "fn" };
      }),
      edges: F.edges.map(function (e) {
        var lab = e.rel === "reads" ? H.R : e.rel === "writes" ? H.W : e.rel === "trigger" ? V.relTrigger : "";
        return { from: e.from, to: e.to, label: lab, dashed: !!e.guess, color: REL_COLOR[e.rel] || "#8c959f", width: 1.6 };
      }),
    };
    def.chain = def.groups.map(function (g) { return g.id; });
    var byId = {};
    F.nodes.forEach(function (n) { byId[n.id] = n; });
    window.__bizmap.viewNodes = F.nodes.map(function (n) { return n.id; });
    mmDraw($("vmap"), def, { minScale: 0.8, node: function (id) {
      var n = byId[id];
      if (!n) return;
      if (n.kind === "data") location.hash = hrefOf(qPath(n.ref, "w"));
      else if (n.kind === "event" && n.key !== F.key) location.hash = hrefOf("topic/" + encodeURIComponent(n.key));
      else if (n.kind === "fn") { var href = viewLink(n.link, n.fk); if (href) location.hash = href; }
    } });
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

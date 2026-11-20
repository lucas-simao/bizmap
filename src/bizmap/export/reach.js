// BizMap export: graph index + table reach (browser twin of export/reach.py).
//
// tableReach() is the multi-hop traversal of the MCP `table_usage` tool:
// functions reading/writing the table (or its columns) and every entrypoint
// reaching them through calls / calls_http / triggers / enqueues, across
// services. When 0.4.3 ships reach through calls_http, swap this function
// (and reach.py) for it; the page only relies on the returned shape.
/* exported BizReach */
var BizReach = (function () {
  "use strict";
  var KINDS = ["calls", "triggers", "enqueues", "calls_http", "reads", "writes", "derives_from"];
  var CALL_KINDS = ["calls", "calls_http", "triggers", "enqueues"];
  var T_ENTRY = 0, T_FUNC = 1, T_TABLE = 2, T_COLUMN = 3;
  var CONF = ["unknown", "heuristic", "inferred", "exact"];

  function csr(n, flat, fromPos) {
    var m = flat.length / 3;
    var off = new Int32Array(n + 1);
    var to = new Int32Array(m);
    var fl = new Uint8Array(m);
    var toPos = 1 - fromPos;
    var i;
    for (i = 0; i < m; i++) off[flat[3 * i + fromPos] + 1]++;
    for (i = 0; i < n; i++) off[i + 1] += off[i];
    var fill = off.slice(0, n);
    for (i = 0; i < m; i++) {
      var a = flat[3 * i + fromPos];
      var k = fill[a]++;
      to[k] = flat[3 * i + toPos];
      fl[k] = flat[3 * i + 2];
    }
    return { off: off, to: to, fl: fl };
  }

  function buildIndex(D) {
    var n = D.nodes.length;
    var G = { D: D, n: n, out: {}, inc: {}, colTable: new Int32Array(n).fill(-1), tableCols: {} };
    KINDS.forEach(function (k) {
      var flat = D.edges[k] || [];
      G.out[k] = csr(n, flat, 0);
      G.inc[k] = csr(n, flat, 1);
    });
    for (var i = 0; i < D.colTable.length; i += 2) {
      var c = D.colTable[i], t = D.colTable[i + 1];
      G.colTable[c] = t;
      (G.tableCols[t] = G.tableCols[t] || []).push(c);
    }
    return G;
  }

  function each(G, dir, kind, v, fn) {
    var x = G[dir][kind], end = x.off[v + 1];
    for (var k = x.off[v]; k < end; k++) fn(x.to[k], x.fl[k]);
  }

  // rank = confidence code + 1 (unknown 1 ... exact 4), as in mcp_server._RANK
  // httpIn (optional Map): every calls_http edge crossed, "from>to" -> [from, to]
  function entrypointsReaching(G, start, httpIn) {
    var best = new Map([[start, 4]]);
    var frontier = [start];
    while (frontier.length) {
      var next = new Set();
      for (var f = 0; f < frontier.length; f++) {
        var cur = frontier[f], bc = best.get(cur);
        for (var c = 0; c < CALL_KINDS.length; c++) {
          var kind = CALL_KINDS[c];
          each(G, "inc", kind, cur, function (from, fl) {
            if (httpIn && kind === "calls_http") httpIn.set(from + ">" + cur, [from, cur]);
            var rank = Math.min(bc, (fl & 3) + 1);
            if (rank > (best.get(from) || 0)) {
              best.set(from, rank);
              next.add(from);
            }
          });
        }
      }
      frontier = Array.from(next).sort(function (a, b) { return a - b; });
    }
    var out = [];
    best.forEach(function (r, id) {
      if (id !== start && G.D.nodes[id][0] === T_ENTRY) out.push([id, r]);
    });
    return out;
  }

  // -> {functions: Map(fn -> {reads: rank, writes: rank}), entrypoints: Map(ep -> rank),
  //     via: Map(ep -> [fn...])}
  function tableReach(G, table) {
    var targets = [table].concat(G.tableCols[table] || []);
    var functions = new Map(), direct = new Map();
    targets.forEach(function (t) {
      ["reads", "writes"].forEach(function (kind) {
        each(G, "inc", kind, t, function (src, fl) {
          var type = G.D.nodes[src][0], rank = (fl & 3) + 1;
          if (type === T_ENTRY) {
            if (rank > (direct.get(src) || 1)) direct.set(src, rank);
            return;
          }
          if (type !== T_FUNC) return;
          var slot = functions.get(src);
          if (!slot) functions.set(src, (slot = {}));
          slot[kind] = Math.max(slot[kind] || 0, rank);
        });
      });
    });
    var eps = new Map(direct), via = new Map(), httpIn = new Map();
    functions.forEach(function (slot, fid) {
      var fnRank = Math.max(slot.reads || 0, slot.writes || 0);
      entrypointsReaching(G, fid, httpIn).forEach(function (pair) {
        var ep = pair[0], rank = Math.min(fnRank, pair[1]);
        eps.set(ep, Math.max(eps.get(ep) || 0, rank));
        var list = via.get(ep);
        if (!list) via.set(ep, (list = []));
        list.push(fid);
      });
    });
    // functions of another service that enter this chain through calls_http
    // (a front end has functions, not entrypoints): {fn -> [entrypoints it calls]}
    var callers = new Map();
    httpIn.forEach(function (pair) {
      var from = pair[0], to = pair[1], n = G.D.nodes;
      if (n[from][0] !== T_FUNC || n[from][1] === n[to][1]) return;
      var list = callers.get(from);
      if (!list) callers.set(from, (list = []));
      if (list.indexOf(to) < 0) list.push(to);
    });
    return { functions: functions, entrypoints: eps, via: via, callers: callers };
  }

  return {
    KINDS: KINDS, CALL_KINDS: CALL_KINDS, CONF: CONF,
    T_ENTRY: T_ENTRY, T_FUNC: T_FUNC, T_TABLE: T_TABLE, T_COLUMN: T_COLUMN,
    buildIndex: buildIndex, each: each,
    entrypointsReaching: entrypointsReaching, tableReach: tableReach,
  };
})();
if (typeof module !== "undefined") module.exports = BizReach;

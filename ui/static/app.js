"use strict";

// ---------------------------------------------------------------- small helpers

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";

/** Build a DOM element. Text is always added as text nodes (never as HTML). */
function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  fill(node, attrs, kids);
  return node;
}
function s(tag, attrs, ...kids) {
  const node = document.createElementNS(SVGNS, tag);
  fill(node, attrs, kids);
  return node;
}
function fill(node, attrs, kids) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.setAttribute("class", value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
}
const put = (id, ...nodes) => $(id).replaceChildren(...nodes.flat(Infinity).filter(Boolean));
const empty = (message, detail) => h("div", {class: "empty"}, h("strong", null, message), detail || "");

const nf = new Intl.NumberFormat("en-US", {maximumFractionDigits: 1});
const num = (value, suffix = "") => (value === null || value === undefined || Number.isNaN(Number(value)) ? "—" : nf.format(Number(value)) + suffix);
const pct = (part, whole) => (whole > 0 ? (100 * part / whole) : 0);
const clock = (ms) => new Date(ms).toISOString().slice(11, 23);

function uptime(seconds) {
  let value = Math.floor(Number(seconds));
  if (!Number.isFinite(value)) return "—";
  const hours = Math.floor(value / 3600); value %= 3600;
  const minutes = Math.floor(value / 60);
  return `${hours ? hours + "h " : ""}${minutes ? minutes + "m " : ""}${value % 60}s`;
}

async function getJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok && response.status !== 500) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-UI-Control": "1"},
    body: JSON.stringify(body || {}),
  });
  return response.json();
}
function setMeta(id, source, timestamp) {
  const node = $(id);
  if (node) node.textContent = `${source} · ${timestamp ? timestamp.replace("T", " ").replace("Z", " UTC") : "no update"}`;
}

// ---------------------------------------------------------------- polling and freshness

const sources = {};          // name -> {ok, at, error, every}
const latest = {};           // name -> last good payload

function poll(name, every, task) {
  sources[name] = {ok: null, at: null, error: "", every};
  let busy = false;
  const tick = async () => {
    if (busy || document.hidden) return;
    busy = true;
    try {
      await task();
      Object.assign(sources[name], {ok: true, at: Date.now(), error: ""});
    } catch (error) {
      Object.assign(sources[name], {ok: false, error: error.message});
    } finally {
      busy = false;
      renderStatus();
    }
  };
  tick();
  setInterval(tick, every);
}

/** Fetch a JSON endpoint; throws (so the source is marked offline) when the relay says ok:false. */
async function load(path) {
  const data = await getJson(path);
  if (data.ok === false) throw new Error(data.error || "source unavailable");
  return data;
}

function renderStatus() {
  const failing = Object.entries(sources).filter(([, v]) => v.ok === false);
  const banner = $("stale-banner");
  if (failing.length === 0) {
    banner.hidden = true;
  } else {
    banner.hidden = false;
    banner.replaceChildren(
      h("strong", null, `${failing.length} live source${failing.length > 1 ? "s are" : " is"} offline. No sample values are shown.`),
      h("ul", null, failing.map(([name, v]) => h("li", null, `${name}: ${v.error}`))));
  }
  const now = Date.now();
  put("freshness", Object.entries(sources).map(([name, v]) => {
    const age = v.at ? (now - v.at) / 1000 : null;
    const cls = v.ok === false ? "bad" : age === null ? "stale" : age > (v.every / 1000) * 3 + 2 ? "stale" : "ok";
    return h("span", {class: `fresh ${cls}`}, `${name} · ${v.ok === false ? "offline" : age === null ? "waiting" : age < 1 ? "now" : Math.round(age) + " s ago"}`);
  }));
  setMeta("u14-meta", "browser poll", new Date().toISOString());
}
setInterval(renderStatus, 1000);

// ---------------------------------------------------------------- U1 topology map

const POS = {
  c1: [10, 40, 100, 46], c2: [10, 110, 100, 46], c3: [10, 180, 100, 46], c4: [10, 250, 100, 46],
  "sw-clients": [150, 140, 110, 56], r1: [305, 128, 130, 80], "sw-lb": [490, 40, 26, 360],
  lb: [580, 50, 170, 64], haproxy: [580, 150, 170, 64], r2: [580, 270, 140, 76],
  "sw-backends": [762, 285, 112, 48],
  b1: [890, 115, 100, 66], b2: [890, 200, 100, 66], b3: [890, 285, 100, 66], b4: [890, 370, 100, 66],
};
const centre = (id, side) => {
  const [x, y, w, hgt] = POS[id];
  return side === "left" ? [x, y + hgt / 2] : side === "right" ? [x + w, y + hgt / 2] : [x + w / 2, y + hgt / 2];
};
const FLOWS = [
  ["client", "M110 63 L150 150 L305 150 L435 150 L503 150 L503 76 L580 76"],
  ["stats", "M110 73 L150 160 L305 160 L435 160 L497 160 L497 90 L580 90"],
  ["forward", "M580 96 L509 96 L509 300 L580 300 L720 300 L762 300 L874 300 L882 300 L882 312 L890 312"],
  ["probe", "M580 106 L513 106 L513 316 L580 316 L720 316 L762 316 L874 316 L882 316 L882 326 L890 326"],
];

function drawMap() {
  const topology = latest.topology;
  if (!topology) return;
  const stats = latest.stats && latest.stats.data;
  const capacity = latest.capacity;
  const running = Object.fromEntries(topology.nodes.map((n) => [n.id, n.running]));
  const totals = stats ? stats.total_requests || 0 : 0;
  const moving = latest.stats && latest.stats.rps > 0;
  const svg = s("svg", {viewBox: "0 0 1000 460", role: "img", "aria-label": "Network topology with flows"});

  const defs = s("defs");
  for (const [name, colour] of [["client", "#0593C6"], ["forward", "#1F9D6B"], ["probe", "#D98E04"], ["stats", "#D83995"]]) {
    defs.append(s("marker", {id: `arrow-${name}`, viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse"},
      s("path", {d: "M0 0 L10 5 L0 10 z", fill: colour})));
  }
  svg.append(defs);

  for (const [a, b] of topology.links) {
    let start, end;
    if (b === "sw-lb") { start = centre(a, "right"); end = [490, start[1]]; }        // R1 meets the bridge
    else if (a === "sw-lb") { end = centre(b, "left"); start = [516, end[1]]; }     // bridge to LB, HAProxy, R2
    else { start = centre(a, "right"); end = centre(b, "left"); }
    svg.append(s("line", {class: "link", x1: start[0], y1: start[1], x2: end[0], y2: end[1]}));
  }
  for (const [name, path] of FLOWS) {
    svg.append(s("path", {class: `flow ${name}${moving ? " moving" : ""}`, d: path, "marker-end": `url(#arrow-${name})`}));
  }

  for (const node of topology.nodes) {
    const [x, y, w, hgt] = POS[node.id];
    const lbSaysDown = stats && stats.per_backend && stats.per_backend[node.id] && stats.per_backend[node.id].healthy === false;
    const state = !node.running ? "down" : lbSaysDown ? "lbdown" : "up";
    const group = s("g", {class: `node ${node.kind === "bridge" ? "bridge" : ""} ${state}`});
    group.append(s("rect", {x, y, width: w, height: hgt, rx: 8}));
    const line = (text, dy, cls = "sub") => group.append(s("text", {class: cls, x: x + w / 2, y: y + dy, "text-anchor": "middle"}, text));
    if (node.id === "sw-lb") {
      group.append(s("text", {class: "sub", x: x + w / 2, y: 30, "text-anchor": "middle"}, "10.0.2.0/24 bridge"));
    } else if (node.kind === "backend") {
      line(node.id, 17, "name");
      line(node.address, 31);
      const info = capacity && capacity.backends.find((b) => b.id === node.id);
      line(info ? `${info.delay_ms || "?"} ms · ${info.cpu || "?"} CPU` : "", 44);
      const requests = stats && stats.per_backend && stats.per_backend[node.id] ? stats.per_backend[node.id].requests : 0;
      if (stats) {
        line(`${num(pct(requests, totals))} % share`, 58);
        group.append(s("rect", {class: "share-bg", x: x + 8, y: y + hgt + 3, width: w - 16, height: 6, rx: 3}));
        group.append(s("rect", {class: "share-bar", x: x + 8, y: y + hgt + 3, width: Math.max(0, (w - 16) * pct(requests, totals) / 100), height: 6, rx: 3}));
      }
    } else {
      line(node.id, hgt > 60 ? 22 : 20, "name");
      line(node.kind === "bridge" ? node.address.replace("bridge ", "") : node.address, hgt > 60 ? 38 : 36);
      if (node.id === "lb") line(stats ? stats.algorithm : "", 54);
      if (node.id === "haproxy") line((latest.haproxy && latest.haproxy.algorithm) || "not answering", 54);
    }
    if (!node.running) group.append(s("text", {class: "tag down", x: x + w - 6, y: y + 13, "text-anchor": "end"}, "DOWN"));
    else if (lbSaysDown) group.append(s("text", {class: "tag lbdown", x: x + w - 6, y: y + 13, "text-anchor": "end"}, "DOWN in LB"));
    svg.append(group);
  }
  put("topology-map", svg);
}

poll("topology", 4000, async () => {
  const data = await load("/api/topology");
  latest.topology = data;
  setMeta("u1-meta", "docker ps", data.timestamp);
  drawMap();
});

// ---------------------------------------------------------------- U2 routing

const HOP_LABELS = {
  "10.0.1.1": "R1, client side (eth1)", "10.0.2.2": "R2, LB side (eth1)", "10.0.3.11": "b1, the destination",
};
poll("routes", 15000, async () => {
  let data;
  try {
    data = await load("/api/routes");
  } catch (error) {
    put("routing-body", empty("No data", error.message));
    throw error;
  }
  const hops = data.hops.map((hop) => h("tr", null, h("td", null, hop.hop), h("td", null, hop.ip),
    h("td", null, hop.ms ? hop.ms + " ms" : ""), h("td", null, HOP_LABELS[hop.ip] || "")));
  const nodes = Object.entries(data.routes).map(([name, route]) => h("details", null,
    h("summary", null, `${name} ip route`), h("pre", {class: "codebox"}, route.ok ? route.stdout : "No data: " + route.stderr)));
  put("routing-body",
    h("h3", null, "traceroute c1 → b1 crosses both routers"),
    h("table", null, h("thead", null, h("tr", null, h("th", null, "Hop"), h("th", null, "Address"), h("th", null, "RTT"), h("th", null, "Device"))),
      h("tbody", null, hops)),
    h("h3", null, "Routing tables"), nodes);
  setMeta("u2-meta", "docker exec ip route / traceroute", data.timestamp);
});

// ---------------------------------------------------------------- U3 network condition

poll("netem", 5000, async () => {
  let data;
  try {
    data = await load("/api/netem");
  } catch (error) {
    put("netem-body", empty("No data", error.message));
    throw error;
  }
  const configured = data.configured.RTT_MS, measured = data.measured.rtt_avg_ms;
  const mismatch = measured !== null && Math.abs(measured - configured) > Math.max(2, configured * 0.15);
  put("netem-body",
    h("div", {class: "tiles"},
      tile("Configured RTT", num(configured, " ms")),
      tile("Measured RTT (ping c1 → LB)", num(measured, " ms"), mismatch ? "bad" : "good"),
      tile("Configured loss", num(data.configured.LOSS_PCT, " %")),
      tile("Measured loss", num(data.measured.loss_pct, " %"))),
    mismatch ? h("div", {class: appState && appState.load_running ? "banner info" : "banner"},
      appState && appState.load_running
        ? "Load is running, so queueing adds delay to the measured RTT. Stop the load to compare."
        : "Measured RTT differs from the configured delay. Is a change still applying?") : null,
    h("h3", null, "netem on R1 (tc qdisc)"),
    h("pre", {class: "codebox"}, data.qdisc_lines.length ? data.qdisc_lines.join("\n") : "No netem delay or loss is active."));
  setMeta("u3-meta", "topo/.netem_state, tc qdisc, ping", data.timestamp);
});

function tile(label, value, cls) {
  return h("div", {class: `kpi ${cls || ""}`}, h("div", {class: "kpi-label"}, label), h("div", {class: "kpi-value small"}, value));
}

// ---------------------------------------------------------------- U4 one request, U5 annotated capture

const HEADER_NOTES = {
  "X-Backend-Id": "which backend served this request",
  Connection: "keep-alive: the client leg stays open for the next request",
  "Content-Type": "response type set by the backend",
};
async function refreshRequest() {
  const data = await getJson("/api/request");
  if (!data.ok) {
    put("request-body", empty("No data", data.error || "the LB did not answer"));
    throw new Error(data.error || "the LB did not answer");
  }
  const rows = Object.entries(data.headers).map(([key, value]) => h("tr", null, h("td", null, key), h("td", null, value), h("td", {class: "hint"}, HEADER_NOTES[key] || "")));
  let backend = "";
  try { backend = JSON.parse(data.body).backend; } catch (error) { /* body is optional */ }
  put("request-body",
    h("p", null, h("span", {class: "badge http"}, data.status), backend ? h("span", null, `  served by ${backend}`) : null),
    h("table", null, h("thead", null, h("tr", null, h("th", null, "Header"), h("th", null, "Value"), h("th", null, "Meaning"))), h("tbody", null, rows)),
    h("pre", {class: "codebox"}, data.body));
  setMeta("u4-meta", "docker exec c1 curl -i", data.timestamp);
}
poll("request", 30000, refreshRequest);
$("request-button").addEventListener("click", () => refreshRequest().catch(() => {}));

const badgeClass = (label) => (label.startsWith("SYN") ? "syn" : label.startsWith("HTTP") ? "http" : label === "FIN" ? "fin" : label === "RST" ? "rst" : "ack");

async function runCapture() {
  const button = $("capture-button");
  button.disabled = true;
  $("capture-status").textContent = "Capturing for 6 s and sending 3 demo requests (decoding takes about 15 s while load is running)...";
  try {
    const data = await post("/api/capture");
    if (!data.ok) throw new Error(data.error || "capture failed");
    const sum = data.summary;
    $("pcap-link").hidden = false;
    $("capture-status").textContent = `Done: ${sum.packets} packets in ${sum.streams} TCP streams.`;
    put("capture-result",
      h("div", {class: "tiles"},
        tile("Client connections", sum.demo_client.connections), tile("SYN on that leg", sum.demo_client.syn),
        tile("HTTP requests", sum.demo_client.requests), tile("Forwarded connections", sum.demo_forward.length),
        tile("Health probes", sum.probes.count), tile("Refused (RST)", sum.refused.count, sum.refused.count ? "bad" : "")),
      h("ul", {class: "notes"}, data.notes.map((note) => h("li", null, note))),
      h("div", {class: "scroll"}, h("table", null,
        h("thead", null, h("tr", null, h("th", null, "Time (s)"), h("th", null, "Leg"), h("th", null, "From → To"), h("th", null, "What"), h("th", null, "Detail"))),
        h("tbody", null, data.events.map((e) => h("tr", null, h("td", {class: "num"}, Number(e.t).toFixed(4)), h("td", null, e.leg),
          h("td", null, `${e.src} → ${e.dst}`), h("td", null, h("span", {class: `badge ${badgeClass(e.label)}`}, e.label)), h("td", null, e.detail)))))));
  } catch (error) {
    $("capture-status").textContent = `Capture unavailable: ${error.message}`;
    put("capture-result", empty("No data", error.message));
  } finally {
    button.disabled = false;
  }
}
$("capture-button").addEventListener("click", runCapture);

// ---------------------------------------------------------------- U6-U7 metrics

function sparkline(values, unit) {
  const clean = values.map((v) => (v === null || v === undefined ? null : Number(v)));
  const points = clean.filter((v) => v !== null);
  if (points.length < 2) return h("div", {class: "empty"}, "Collecting samples...");
  const max = Math.max(...points, 1), W = 300, H = 70, pad = 6;
  const step = (W - pad * 2) / (clean.length - 1);
  const y = (v) => H - pad - (v / max) * (H - pad * 2 - 8);
  let line = "", area = "", last = null;
  clean.forEach((v, i) => {
    if (v === null) return;
    const x = pad + i * step;
    line += `${line ? "L" : "M"}${x.toFixed(1)} ${y(v).toFixed(1)} `;
    last = [x, y(v), v];
  });
  area = `${line} L${last[0].toFixed(1)} ${H - pad} L${pad} ${H - pad} Z`;
  return h("div", null,
    s("svg", {viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": `${unit} history`},
      s("path", {class: "area", d: area}), s("path", {class: "line", d: line}),
      s("ellipse", {class: "now", cx: last[0], cy: last[1], rx: 3.5, ry: 3.5})),
    h("div", {class: "chart-label"}, h("span", null, `now ${nf.format(last[2])}`), h("span", null, `max ${nf.format(max)} · last ${points.length} s`)));
}

poll("stats", 1000, async () => {
  let envelope;
  try {
    envelope = await load("/api/stats");
  } catch (error) {
    const stopped = appState && !appState.lb_running;
    const reason = stopped ? "The own load balancer is stopped (HAProxy is the active system). Its live numbers are in U9 below; choose Own LB or Both in U13 to see this panel." : error.message;
    put("metrics-kpis", empty(stopped ? "Own LB stopped" : "No data", reason));
    put("backend-table", h("tr", null, h("td", {colspan: 6, class: "empty"}, stopped ? "Own LB stopped" : "No data")));
    put("chart-rps"); put("chart-p95");
    delete latest.stats;
    drawMap();
    throw error;
  }
  latest.stats = envelope;
  const data = envelope.data;
  const history = envelope.history;
  put("metrics-kpis", [
    ["Algorithm", data.algorithm], ["Uptime", uptime(data.uptime_s)], ["Total requests", nf.format(data.total_requests || 0)],
    ["Requests/s", envelope.rps === null ? "—" : nf.format(envelope.rps)], ["Errors", nf.format(data.errors || 0)],
    ["p50 / p95 / p99", `${num(data.latency_ms.p50)} / ${num(data.latency_ms.p95)} / ${num(data.latency_ms.p99)} ms`],
  ].map(([label, value], i) => h("div", {class: `kpi ${label === "Errors" && data.errors > 0 ? "bad" : ""}`}, h("div", {class: "kpi-label"}, label),
    h("div", {class: `kpi-value ${i === 5 || i === 0 ? "small" : ""}`}, value))));
  put("chart-rps", sparkline(history.map((p) => p.rps), "requests per second"));
  put("chart-p95", sparkline(history.map((p) => p.p95), "p95 latency"));
  put("backend-table", Object.entries(data.per_backend || {}).map(([id, b]) => {
    const share = pct(b.requests || 0, data.total_requests || 0);
    return h("tr", null, h("th", null, id), h("td", {class: "num"}, nf.format(b.requests || 0)),
      h("td", null, h("div", {class: "share-cell"}, h("div", {class: "bar", style: "flex:1"}, h("span", {style: `width:${share}%`})), h("small", null, nf.format(share) + " %"))),
      h("td", {class: "num"}, nf.format(b.active || 0)),
      h("td", {class: b.healthy ? "up" : "down"}, b.healthy ? "● UP" : "● DOWN"), h("td", {class: "num"}, nf.format(b.errors || 0)));
  }));
  setMeta("u6-meta", "relay sampler: GET :9090/stats once per second", envelope.timestamp);
  drawMap();
  applyState();
});

// ---------------------------------------------------------------- U8 failover

poll("failover", 2000, async () => {
  const data = await load("/api/failover");
  const detect = (d) => (d ? `${d.seconds.toFixed(2)} s` : data.kill_ms ? "waiting..." : "—");
  const backends = latest.stats ? Object.entries(latest.stats.data.per_backend || {}) : [];
  put("failover-body",
    h("div", {class: "tiles"},
      tile("Last kill of b3", data.kill_ms ? clock(data.kill_ms) + " UTC" : "none yet"),
      tile("Own LB detection", detect(data.own), data.own ? "good" : ""),
      tile("HAProxy detection", detect(data.haproxy), data.haproxy ? "good" : ""),
      tile("Failed requests (≈)", data.failed_requests_approx === null ? "—" : data.failed_requests_approx, data.failed_requests_approx ? "bad" : "")),
    backends.length ? h("p", null, backends.map(([id, b]) => h("span", {class: `badge ${b.healthy ? "http" : "rst"}`, style: "margin-right:6px"}, `${id} ${b.healthy ? "UP" : "DOWN"}`))) : null,
    h("h3", null, "Health events (own LB log and HAProxy log)"),
    data.events.length === 0 ? h("div", {class: "empty"}, "No health changes yet. Use Kill b3 in U13.") :
      h("div", {class: "timeline"}, data.events.slice().reverse().slice(0, 12).map((e) => h("div", {class: "event"},
        h("time", null, clock(e.ts_ms)), h("span", null, e.system === "own" ? "own LB" : "HAProxy"),
        h("strong", {class: e.state === "DOWN" ? "down" : "up"}, `${e.backend} ${e.state}`),
        h("span", null, (e.reason || "") + (data.kill_ms && e.ts_ms >= data.kill_ms && e.ts_ms - data.kill_ms < 600000 ? `  (+${((e.ts_ms - data.kill_ms) / 1000).toFixed(2)} s after kill)` : ""))))),
    h("p", {class: "hint"}, "Failed requests are the growth of the LB error counter between the kill and detection (1 s sampling)."));
  setMeta("u8-meta", "logs/lb.jsonl, docker logs haproxy, kill timestamp", data.timestamp);
});

// ---------------------------------------------------------------- U9 HAProxy

poll("haproxy", 3000, async () => {
  let data;
  try {
    data = await load("/api/haproxy");
  } catch (error) {
    put("haproxy-body", empty("No data", error.message));
    throw error;
  }
  latest.haproxy = data;
  const own = latest.stats ? latest.stats.data : null;
  const total = data.servers.reduce((sum, row) => sum + Number(row.stot || 0), 0);
  const sum = data.summary || {total: 0, scur: 0, rtime_ms: 0, errors: 0};
  const ownRps = latest.stats ? latest.stats.rps : null;
  // Side by side: only when both systems answer (use "Both (demo)" in U13). HAProxy has no percentiles, so it shows the average response time instead.
  const compare = own ? h("div", null,
    h("h3", null, "Side by side: own LB vs HAProxy (live)"),
    h("table", null,
      h("thead", null, h("tr", null, h("th", null, "Measure"), h("th", {class: "num"}, "Own LB"), h("th", {class: "num"}, "HAProxy"))),
      h("tbody", null,
        [["Requests per second", ownRps === null ? "—" : nf.format(ownRps), data.rps === null ? "—" : nf.format(data.rps)],
         ["Total requests", nf.format(own.total_requests || 0), nf.format(sum.total)],
         ["Errors", nf.format(own.errors || 0), nf.format(sum.errors)],
         ["Latency p50 / p95 / p99", `${num(own.latency_ms.p50)} / ${num(own.latency_ms.p95)} / ${num(own.latency_ms.p99)} ms`, "not provided by HAProxy"],
         ["Average response time", "—", `${nf.format(sum.rtime_ms)} ms`]]
          .map(([m, a, b]) => h("tr", null, h("th", null, m), h("td", {class: "num"}, a), h("td", {class: "num"}, b))))),
    h("p", {class: "hint"}, "Demo mode: when both run at once they share the host CPU and the load is split, so use this view to watch behaviour, not to quote numbers. The report's numbers come from scripted runs with one system at a time."))
    : h("p", {class: "hint"}, "Own LB is stopped, so only HAProxy is shown. Choose Both (demo) in U13 for a side-by-side comparison.");
  put("haproxy-body",
    h("p", null, h("span", {class: "badge http"}, `algorithm: ${data.algorithm || "?"}`), "  ", own ? h("span", {class: "badge syn"}, `own LB: ${own.algorithm}`) : null),
    h("div", {class: "tiles"},
      tile("HAProxy requests/s", data.rps === null ? "—" : nf.format(data.rps)), tile("HAProxy total requests", nf.format(sum.total)),
      tile("Active sessions", nf.format(sum.scur)), tile("Avg response", `${nf.format(sum.rtime_ms)} ms`)),
    h("figure", null, h("figcaption", null, "HAProxy requests per second"),
      h("div", {class: "chart"}, sparkline((data.history || []).map((p) => p.rps), "HAProxy requests per second"))),
    compare,
    h("table", null,
      h("thead", null, h("tr", null, h("th", null, "Server"), h("th", null, "Status"), h("th", {class: "num"}, "Sessions"), h("th", null, "HAProxy share"),
        h("th", null, "Own LB share"), h("th", {class: "num"}, "Avg resp"))),
      h("tbody", null, data.servers.map((row) => {
        const share = pct(Number(row.stot || 0), total);
        const ownShare = own && own.per_backend[row.svname] ? pct(own.per_backend[row.svname].requests, own.total_requests) : null;
        return h("tr", null, h("th", null, row.svname), h("td", {class: row.status === "UP" ? "up" : "down"}, row.status), h("td", {class: "num"}, nf.format(Number(row.stot || 0))),
          h("td", null, h("div", {class: "share-cell"}, h("div", {class: "bar", style: "flex:1"}, h("span", {style: `width:${share}%`})), h("small", null, nf.format(share) + " %"))),
          h("td", null, ownShare === null ? "—" : nf.format(ownShare) + " %"), h("td", {class: "num"}, row.rtime ? row.rtime + " ms" : "—"));
      }))),
    h("p", {class: "hint"}, "HAProxy statistics give counters, status and an average response time only. p50/p95/p99 are not invented."));
  setMeta("u9-meta", "HAProxy :8404/;csv via c1", data.timestamp);
  drawMap();
});

// ---------------------------------------------------------------- U10 capacity

poll("capacity", 6000, async () => {
  const data = await load("/api/backends");
  latest.capacity = data;
  put("capacity-body", h("table", null,
    h("thead", null, h("tr", null, h("th", null, "Backend"), h("th", null, "Address"), h("th", {class: "num"}, "Delay"), h("th", null, "CPU (config / live)"), h("th", null, "GET /health"))),
    h("tbody", null, data.backends.map((b) => h("tr", null, h("th", null, b.id), h("td", null, `${b.address}:8000`), h("td", {class: "num"}, b.delay_ms ? b.delay_ms + " ms" : "No data"),
      h("td", null, `${b.cpu || "?"} / ${b.live_cpu === undefined ? "?" : b.live_cpu}`),
      h("td", {class: b.health.ok ? "up" : "down"}, b.health.ok ? `UP · ${num(b.health.ms)} ms` : "no answer"))))));
  setMeta("u10-meta", "backends/.env, docker inspect, GET /health via c1", data.timestamp);
});

// ---------------------------------------------------------------- U11 results, U12 environment

poll("results", 15000, async () => {
  const data = await load("/api/results");
  const banner = $("experiment-banner");
  banner.hidden = false;
  if (data.records.length === 0) {
    banner.className = "banner info";
    banner.textContent = "No experiment summary files yet. Nothing is charted.";
    put("experiment-body", empty("No data"));
  } else {
    banner.className = `banner ${data.dry_run ? "dry" : "info"}`;
    banner.textContent = data.dry_run ? "DRY RUN - NOT REPORT DATA (results/checkpoint2: one run per system)" : `${data.records.length} experiment runs`;
    put("experiment-body", h("div", {class: "scroll"}, h("table", null,
      h("thead", null, h("tr", null, ["System", "Algo", "Conc", "RTT", "Pre rps", "Pre p95", "Post rps", "Post p95", "Detect", "Valid"].map((x) => h("th", null, x)))),
      h("tbody", null, data.records.map((r) => h("tr", null, h("th", null, r.system), h("td", null, r.algorithm), h("td", {class: "num"}, r.concurrency), h("td", {class: "num"}, r.rtt_ms + " ms"),
        h("td", {class: "num"}, num(r.windows && r.windows.pre && r.windows.pre.rps)), h("td", {class: "num"}, num(r.windows && r.windows.pre && r.windows.pre.p95_ms, " ms")),
        h("td", {class: "num"}, num(r.windows && r.windows.post && r.windows.post.rps)), h("td", {class: "num"}, num(r.windows && r.windows.post && r.windows.post.p95_ms, " ms")),
        h("td", {class: "num"}, num(r.detection_s, " s")), h("td", {class: r.valid ? "up" : "down", title: r.invalid_reason || ""}, r.valid ? "yes" : "no")))))));
  }
  setMeta("u11-meta", "results/**/summary.json", data.timestamp);
});

poll("environment", 30000, async () => {
  const data = await load("/api/environment");
  const files = Object.entries(data.environment_files);
  put("environment-body",
    h("table", null, h("thead", null, h("tr", null, h("th", null, "Container"), h("th", {class: "num"}, "CPU limit (live)"))),
      h("tbody", null, Object.entries(data.container_cpu_limits).map(([name, value]) => h("tr", null, h("th", null, name), h("td", {class: "num"}, value === "unlimited" ? "unlimited" : value + " CPU"))))),
    h("p", {class: "hint"}, data.note),
    files.map(([path, values]) => h("details", null, h("summary", null, path),
      h("pre", {class: "codebox"}, Object.entries(values).map(([k, v]) => `${k}: ${v}`).join("\n")))));
});

// ---------------------------------------------------------------- U13 controls

let appState = null;
poll("state", 3000, async () => {
  appState = await load("/api/state");
  applyState();
});

function applyState() {
  if (!appState) return;
  const st = appState;
  const own = (latest.stats && latest.stats.data.algorithm) || st.own_algorithm;
  document.querySelectorAll("[data-control]").forEach((button) => {
    const d = button.dataset;
    let on = false, disabled = false;
    if (d.control === "rtt") on = Number(d.rtt) === Number(st.rtt_ms);
    if (d.control === "algorithm") on = d.algorithm === own;
    if (d.control === "haproxy-algorithm") on = d.algorithm === st.haproxy_algorithm;
    if (d.control === "active-system") {
      const both = st.lb_running && st.haproxy_running;
      on = d.system === "both" ? both : d.system === "own" ? st.lb_running && !both : st.haproxy_running && !both;
    }
    if (d.control === "start-load") disabled = st.load_running;
    if (d.control === "stop-load") disabled = !st.load_running;
    if (d.control === "kill-b3") disabled = !st.b3_running;
    if (d.control === "start-b3") disabled = st.b3_running;
    button.classList.toggle("active", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
    if (!button.classList.contains("busy")) button.disabled = disabled;
  });
}

function logControl(ok, message) {
  const list = $("control-log");
  if (list.firstElementChild && list.firstElementChild.classList.contains("hint")) list.replaceChildren();
  list.prepend(h("li", {class: ok ? "" : "fail"}, h("time", null, new Date().toISOString().slice(11, 19)), message));
  while (list.children.length > 8) list.lastElementChild.remove();
}

async function runControl(button, quiet) {
  const d = button.dataset;
  const body = d.control === "rtt" ? {rtt_ms: d.rtt} : d.control === "active-system" ? {system: d.system} : d.algorithm ? {algorithm: d.algorithm} : {};
  button.classList.add("busy"); button.disabled = true;
  let result;
  try {
    result = await post(`/api/control/${d.control}`, body);
  } catch (error) {
    result = {ok: false, message: `Control failed: ${error.message}`};
  }
  button.classList.remove("busy");
  if (!quiet) logControl(result.ok, result.message || result.error || "done");
  cacheBust();
  await Promise.allSettled([refreshState(), refreshTopologyNow()]);
  return result;
}
const cacheBust = () => { appState = null; };
async function refreshState() { appState = await getJson("/api/state"); applyState(); }
async function refreshTopologyNow() { latest.topology = await getJson("/api/topology"); drawMap(); }

document.querySelectorAll("[data-control]").forEach((button) => button.addEventListener("click", () => runControl(button)));

// guided failover demo: fixed steps, built from the same fixed controls
const DEMO = [
  {text: "Start load: 4 clients x 50 connections", control: "start-load", wait: 8},
  {text: "Kill b3 and watch U8 detect it", control: "kill-b3", wait: 10},
  {text: "Restore b3 and watch it return to UP", control: "start-b3", wait: 6},
  {text: "Stop load", control: "stop-load", wait: 0},
];
let demoCancelled = false;
function renderSteps(statuses = {}) {
  put("demo-steps", DEMO.map((step, i) => h("li", {class: statuses[i] ? statuses[i].cls : ""},
    h("span", {class: "state"}, statuses[i] ? statuses[i].label : "pending"), h("span", null, step.text))));
}
renderSteps();
const sleep = (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000));

async function runDemo() {
  if (appState && !appState.lb_running) { logControl(false, "Activate the own LB first (Active system)."); return; }
  demoCancelled = false;
  $("demo-run").disabled = true; $("demo-cancel").hidden = false;
  const statuses = {};
  for (let i = 0; i < DEMO.length && !demoCancelled; i++) {
    statuses[i] = {cls: "running", label: "running"}; renderSteps(statuses);
    const button = document.querySelector(`[data-control="${DEMO[i].control}"]`);
    const result = await runControl(button, true);
    logControl(result.ok, `${DEMO[i].text}: ${result.message || result.error}`);
    if (!result.ok) { statuses[i] = {cls: "failed", label: "failed"}; renderSteps(statuses); break; }
    for (let left = DEMO[i].wait; left > 0 && !demoCancelled; left--) {
      statuses[i] = {cls: "running", label: `${left} s`}; renderSteps(statuses); await sleep(1);
    }
    statuses[i] = {cls: "done", label: "done"}; renderSteps(statuses);
  }
  if (demoCancelled) {
    await runControl(document.querySelector('[data-control="stop-load"]'), true);
    await runControl(document.querySelector('[data-control="start-b3"]'), true);
    logControl(true, "Demo cancelled: load stopped and b3 restored.");
  }
  $("demo-run").disabled = false; $("demo-cancel").hidden = true;
}
$("demo-run").addEventListener("click", runDemo);
$("demo-cancel").addEventListener("click", () => { demoCancelled = true; });

// ---------------------------------------------------------------- relay status, nav, annotations

async function relayStatus() {
  const pill = $("relay-status");
  try {
    await getJson("/api/status");
    pill.textContent = "Relay online"; pill.className = "status-pill ok";
  } catch (error) {
    pill.textContent = "Relay offline"; pill.className = "status-pill offline";
  }
}
relayStatus();
setInterval(relayStatus, 5000);
setMeta("u13-meta", "fixed relay controls", new Date().toISOString());

const sections = [...document.querySelectorAll(".panel-grid > section")];
const observer = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    document.querySelectorAll(".sidebar a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${entry.target.id}`));
  }
}, {rootMargin: "-20% 0px -65% 0px"});
sections.forEach((section) => observer.observe(section));

(async function annotations() {
  try {
    const notes = await getJson("/api/annotations");
    const panels = {U1: "topology", U2: "routing", U3: "network", U4: "request", U5: "request", U6: "metrics", U7: "metrics", U8: "health",
      U9: "reference", U10: "backends", U11: "experiments", U12: "experiments", U13: "demo", U14: "freshness-panel"};
    for (const [key, id] of Object.entries(panels)) {
      const body = document.querySelector(`#${id} .panel-body`);
      if (body && notes[key]) body.append(h("div", {class: "annotation", "data-annotation": key}, `${key}: ${notes[key]}`));
    }
  } catch (error) { /* annotations are optional */ }
})();

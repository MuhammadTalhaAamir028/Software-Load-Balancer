"use strict";

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("en-US");
let previousTotal = null;
let previousTotalTime = null;

function setMeta(id, source, timestamp) {
  const node = $(id);
  if (!node) return;
  node.textContent = `${source} · ${timestamp ? timestamp.replace("T", " ").replace("Z", " UTC") : "no update"}`;
}

async function getJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function fmtPercent(part, whole) {
  return whole > 0 ? `${(100 * part / whole).toFixed(1)} %` : "0.0 %";
}

function fmtUptime(seconds) {
  if (!Number.isFinite(Number(seconds))) return "No data";
  let value = Math.floor(Number(seconds));
  const hours = Math.floor(value / 3600);
  value %= 3600;
  const minutes = Math.floor(value / 60);
  const secs = value % 60;
  return `${hours ? hours + "h " : ""}${minutes ? minutes + "m " : ""}${secs}s`;
}

async function refreshStatus() {
  try {
    await getJson("/api/status");
    $("relay-status").textContent = "Relay online";
    $("relay-status").className = "status-pill";
    $("stale-banner").hidden = true;
  } catch (error) {
    $("relay-status").textContent = "Relay offline";
    $("relay-status").className = "status-pill offline";
    $("stale-banner").hidden = false;
  }
}

async function refreshTopology() {
  try {
    const data = await getJson("/api/topology");
    $("topology-body").replaceChildren();
    for (const node of data.nodes) {
      const card = document.createElement("div");
      card.className = `node ${node.running ? "running" : "offline"}`;
      const name = document.createElement("strong");
      name.textContent = node.id;
      const address = document.createElement("span");
      address.textContent = `${node.address} · ${node.running ? "UP" : "DOWN"}`;
      card.append(name, address);
      $("topology-body").appendChild(card);
    }
    setMeta("u1-meta", "docker ps / docker inspect", data.timestamp);
  } catch (error) {
    $("topology-body").textContent = "No data";
  }
}

async function refreshRoutes() {
  try {
    const data = await getJson("/api/routes");
    const lines = [];
    for (const [node, route] of Object.entries(data.routes)) {
      lines.push(`--- ${node} ip route ---`);
      lines.push(route.ok ? route.stdout : "No data");
    }
    lines.push("--- traceroute c1 -> b1 ---");
    lines.push(data.traceroute.ok ? data.traceroute.stdout : "No data");
    $("routing-body").textContent = lines.join("\n");
    setMeta("u2-meta", "docker exec ip route / traceroute", data.timestamp);
  } catch (error) {
    $("routing-body").textContent = "No data";
  }
}

async function refreshNetem() {
  try {
    const data = await getJson("/api/netem");
    const config = data.configured;
    $("netem-body").textContent =
      `Configured RTT: ${config.RTT_MS} ms\n` +
      `Configured loss: ${config.LOSS_PCT} %\n\n` +
      (data.ping.ok ? data.ping.stdout : "Measured ping: no data") + "\n\n" +
      (data.qdisc.ok ? data.qdisc.stdout : "R1 qdisc: no data");
    setMeta("u3-meta", "netem state / tc qdisc / ping", data.timestamp);
  } catch (error) {
    $("netem-body").textContent = "No data";
  }
}

async function refreshRequest() {
  try {
    const data = await getJson("/api/request");
    $("request-body").textContent = data.ok ? data.curl.stdout : "No data";
    setMeta("u4-meta", "docker exec c1 curl", data.timestamp);
  } catch (error) {
    $("request-body").textContent = "No data";
  }
}

async function refreshStats() {
  try {
    const envelope = await getJson("/api/stats");
    if (!envelope.ok) throw new Error(envelope.error || "stats unavailable");
    const data = envelope.data;
    const now = Date.now();
    let requestsPerSecond = "No data";
    if (previousTotal !== null && previousTotalTime !== null) {
      const elapsed = Math.max((now - previousTotalTime) / 1000, 0.001);
      requestsPerSecond = ((data.total_requests - previousTotal) / elapsed).toFixed(1) + " rps";
    }
    previousTotal = data.total_requests;
    previousTotalTime = now;
    const kpis = [
      ["Algorithm", data.algorithm || "No data"],
      ["Uptime", fmtUptime(data.uptime_s)],
      ["Total requests", fmt.format(data.total_requests || 0)],
      ["Requests/s", requestsPerSecond],
      ["Errors", fmt.format(data.errors || 0)],
      ["p50 / p95 / p99", `${data.latency_ms.p50} / ${data.latency_ms.p95} / ${data.latency_ms.p99} ms`],
    ];
    $("metrics-kpis").replaceChildren();
    for (const [label, value] of kpis) {
      const card = document.createElement("div");
      card.className = "kpi";
      const labelNode = document.createElement("div");
      labelNode.className = "kpi-label";
      labelNode.textContent = label;
      const valueNode = document.createElement("div");
      valueNode.className = "kpi-value";
      valueNode.textContent = value;
      card.append(labelNode, valueNode);
      $("metrics-kpis").appendChild(card);
    }

    $("backend-table").replaceChildren();
    for (const [id, backend] of Object.entries(data.per_backend || {})) {
      const row = document.createElement("tr");
      const cells = [
        id,
        fmt.format(backend.requests || 0),
        fmtPercent(backend.requests || 0, data.total_requests || 0),
        fmt.format(backend.active || 0),
        `${backend.healthy ? "● UP" : "● DOWN"}`,
        fmt.format(backend.errors || 0),
      ];
      cells.forEach((value, index) => {
        const cell = document.createElement(index === 0 ? "th" : "td");
        cell.textContent = value;
        if (index === 2 || index === 3 || index === 5) cell.className = "num";
        if (index === 4) cell.className = backend.healthy ? "up" : "down";
        row.appendChild(cell);
      });
      $("backend-table").appendChild(row);
    }

    const events = (data.health_events || []).slice(-10).reverse();
    $("health-body").replaceChildren();
    if (events.length === 0) {
      $("health-body").textContent = "No health changes yet";
    } else {
      for (const event of events) {
        const row = document.createElement("div");
        row.className = "event";
        const time = document.createElement("span");
        time.textContent = event.ts;
        const state = document.createElement("strong");
        state.textContent = event.healthy ? "UP" : "DOWN";
        state.className = event.healthy ? "up" : "down";
        const reason = document.createElement("span");
        reason.textContent = `${event.backend}: ${event.reason}`;
        row.append(time, state, reason);
        $("health-body").appendChild(row);
      }
    }
    setMeta("u6-meta", "GET http://10.0.2.10:9090/stats", envelope.timestamp);
    setMeta("u8-meta", "/stats health_events + lb.jsonl", envelope.timestamp);
  } catch (error) {
    $("metrics-kpis").textContent = "No data";
    $("backend-table").innerHTML = '<tr><td colspan="6" class="empty">No data</td></tr>';
    $("health-body").textContent = "No data";
    $("stale-banner").hidden = false;
  }
}

async function refreshHaproxy() {
  try {
    const data = await getJson("/api/haproxy");
    $("haproxy-body").replaceChildren();
    if (!data.csv || data.csv.length === 0) {
      $("haproxy-body").textContent = "No data";
    } else {
      const table = document.createElement("table");
      const thead = document.createElement("thead");
      thead.innerHTML = "<tr><th>Server</th><th>Status</th><th>Requests</th></tr>";
      table.appendChild(thead);
      const tbody = document.createElement("tbody");
      for (const row of data.csv) {
        if (!row.svname || ["FRONTEND", "BACKEND"].includes(row.svname)) continue;
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${row.svname}</td><td>${row.status || "No data"}</td><td>${row.stot || "0"}</td>`;
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      $("haproxy-body").appendChild(table);
    }
    setMeta("u9-meta", "HAProxy :8404/;csv / docker logs", data.timestamp);
  } catch (error) {
    $("haproxy-body").textContent = "No data";
  }
}

async function refreshCapacity() {
  try {
    const data = await getJson("/api/backends");
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>Backend</th><th>Address</th><th>Delay</th><th>CPU</th><th>Health timing</th></tr></thead>";
    const tbody = document.createElement("tbody");
    for (const backend of data.backends) {
      const health = backend.health.ok
        ? "UP"
        : "no data";
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${backend.id}</td><td>${backend.address}:8000</td>` +
        `<td>${backend.delay_ms ? backend.delay_ms + " ms" : "No data"}</td>` +
        `<td>${backend.cpu ? backend.cpu : "No data"}</td><td>${health}</td>`;
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    $("capacity-body").replaceChildren(table);
    setMeta("u10-meta", "backends/.env / GET /health", data.timestamp);
  } catch (error) {
    $("capacity-body").textContent = "No data";
  }
}

async function refreshResults() {
  try {
    const data = await getJson("/api/results");
    const records = data.records || [];
    if (records.length === 0) {
      $("experiment-banner").textContent = "U11: no experiment summary files. No charted values.";
      $("experiment-body").textContent = "U12: environment data unavailable or partial.";
      setMeta("u11-meta", "results/**/summary.json", data.timestamp);
      return;
    }
    $("experiment-banner").textContent = "DRY RUN - NOT REPORT DATA";
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>System</th><th>Pre RPS</th><th>Pre p95</th><th>Post RPS</th><th>Post p95</th><th>Detection</th><th>Valid</th></tr></thead>";
    const tbody = document.createElement("tbody");
    for (const record of records) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${record.system}</td><td>${record.windows?.pre?.rps ?? "No data"}</td>` +
        `<td>${record.windows?.pre?.p95_ms ?? "No data"} ms</td>` +
        `<td>${record.windows?.post?.rps ?? "No data"}</td>` +
        `<td>${record.windows?.post?.p95_ms ?? "No data"} ms</td>` +
        `<td>${record.detection_s ?? "No data"} s</td><td>${record.valid ? "yes" : "no"}</td>`;
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    $("experiment-body").replaceChildren(table);
    setMeta("u11-meta", "results/checkpoint2 - dry run", data.timestamp);
  } catch (error) {
    $("experiment-banner").textContent = "U11: no experiment summary files.";
    $("experiment-body").textContent = "U12: integrity data unavailable.";
  }
}

async function refreshEnvironment() {
  try {
    const data = await getJson("/api/environment");
    const lines = [];
    for (const [name, limit] of Object.entries(data.container_cpu_limits || {})) {
      lines.push(`${name}: ${limit === "unlimited" ? "unlimited" : limit + " CPU"}`);
    }
    for (const [path, values] of Object.entries(data.environment_files || {})) {
      lines.push(`--- ${path} ---`);
      for (const [key, value] of Object.entries(values)) lines.push(`${key}: ${value}`);
    }
    $("environment-body").replaceChildren();
    const pre = document.createElement("pre");
    pre.className = "codebox";
    pre.textContent = lines.length ? lines.join("\n") : "Environment data unavailable.";
    $("environment-body").appendChild(pre);
  } catch (error) {
    $("environment-body").textContent = "Environment data unavailable.";
  }
}

async function runControl(button) {
  const control = button.dataset.control;
  let path = `/api/control/${control}`;
  const options = {method: "POST"};
  if (control === "rtt") {
    path = "/api/control/rtt";
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify({rtt_ms: button.dataset.rtt});
  } else if (control === "algorithm") {
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify({algorithm: button.dataset.algorithm});
  } else if (control === "active-system") {
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify({system: button.dataset.system});
  } else if (control === "haproxy-algorithm") {
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify({algorithm: button.dataset.algorithm});
  }
  $("control-result").textContent = "Running fixed control...";
  try {
    const response = await fetch(path, options);
    const data = await response.json();
    $("control-result").textContent = data.ok
      ? (data.result?.stdout || data.result?.stderr || "Control completed")
      : (data.error || data.result?.stderr || "Control failed");
  } catch (error) {
    $("control-result").textContent = `Control failed: ${error.message}`;
  }
  await refreshNetem();
  await refreshTopology();
}

async function runCapture() {
  $("capture-result").textContent = "Starting 5-second capture...";
  try {
    const data = await getJson("/api/capture");
    if (!data.ok) throw new Error(data.capture?.stderr || "capture failed");
    $("capture-result").textContent = "Capture complete";
    $("packet-body").textContent = data.packets?.ok
      ? (data.packets.stdout || "No matching packet rows.")
      : `Packet decode unavailable: ${data.packets?.stderr || "tshark unavailable"}`;
    const link = $("pcap-link");
    link.hidden = false;
    link.href = data.pcap;
    link.textContent = "Download .pcap";
  } catch (error) {
    $("capture-result").textContent = `Capture unavailable: ${error.message}`;
  }
}

async function refreshAll() {
  await refreshStatus();
  await refreshTopology();
  await refreshRoutes();
  await refreshNetem();
  await refreshRequest();
  await refreshStats();
  await refreshHaproxy();
  await refreshCapacity();
  await refreshResults();
  await refreshEnvironment();
  await refreshAnnotations();
  $("banner-time").textContent = new Date().toISOString().replace(".000Z", "Z");
  setMeta("u13-meta", "fixed relay controls / panel poll", new Date().toISOString());
}

async function refreshAnnotations() {
  try {
    const annotations = await getJson("/api/annotations");
    const panelIds = {
      "U1": "topology", "U2": "routing", "U3": "network", "U4": "request",
      "U5": "request", "U6": "metrics", "U7": "metrics", "U8": "health",
      "U9": "reference", "U10": "backends", "U11": "experiments",
      "U12": "experiments", "U13": "demo", "U14": "demo",
    };
    for (const [panel, sectionId] of Object.entries(panelIds)) {
      const section = $(sectionId);
      const body = section?.querySelector(".panel-body");
      if (!body || body.querySelector(`[data-annotation="${panel}"]`)) continue;
      const note = document.createElement("div");
      note.className = "annotation";
      note.dataset.annotation = panel;
      note.textContent = `${panel}: ${annotations[panel] || "Annotation unavailable"}`;
      body.appendChild(note);
    }
  } catch (error) {
    // The dashboard remains functional without optional annotation text.
  }
}

document.querySelectorAll("[data-control]").forEach((button) => {
  button.addEventListener("click", () => runControl(button));
});
$("capture-button").addEventListener("click", runCapture);

refreshAll();
setInterval(refreshStats, 1000);
setInterval(refreshTopology, 5000);
setInterval(refreshNetem, 5000);
setInterval(refreshHaproxy, 5000);

const state = {
  mode: "live",
  layout: null,
  live: null,
  tool: "sensor",
  drag: null,
  pending: null,
  pendingKind: "shelf",
  pin: null,
  heat: true,
  saving: false,
  session: "A",
};

const canvas = document.getElementById("floor");
const ctx = canvas.getContext("2d");

function $(id) { return document.getElementById(id); }

function showError(text) {
  const el = $("error");
  el.hidden = !text;
  el.textContent = text || "";
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

function roomOf() {
  return state.layout?.room || { width_m: 6, depth_m: 4 };
}

function pad() { return 48; }

function resize() {
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
}

function outsideMeters() {
  const room = roomOf();
  const router = state.layout?.router;
  let extra = 1.6;
  if (router) {
    extra = Math.max(
      extra,
      -Number(router.x),
      Number(router.x) - room.width_m,
      -Number(router.y),
      Number(router.y) - room.depth_m,
      0
    ) + 0.6;
  }
  return extra;
}

function map() {
  const room = roomOf();
  const extra = outsideMeters();
  const spanW = room.width_m + extra * 2;
  const spanH = room.depth_m + extra * 2;
  const margin = pad();
  const availW = Math.max(1, canvas.width - margin * 2);
  const availH = Math.max(1, canvas.height - margin * 2);
  const aspect = spanW / spanH;
  let w = availW;
  let h = w / aspect;
  if (h > availH) {
    h = availH;
    w = h * aspect;
  }
  const x = (canvas.width - w) / 2;
  const y = (canvas.height - h) / 2;
  return {
    toCanvas(mx, my) {
      return [
        x + ((mx + extra) / spanW) * w,
        y + (1 - (my + extra) / spanH) * h,
      ];
    },
    toRoom(px, py) {
      return {
        x: ((px - x) / w) * spanW - extra,
        y: (1 - (py - y) / h) * spanH - extra,
      };
    },
    contains(px, py) {
      return px >= x && py >= y && px <= x + w && py <= y + h;
    },
    rect() { return { x, y, w, h }; },
  };
}

function pointerRoom(event, allowOutside) {
  const rect = canvas.getBoundingClientRect();
  const scale = canvas.width / rect.width;
  const px = (event.clientX - rect.left) * scale;
  const py = (event.clientY - rect.top) * scale;
  const view = map();
  if (!view.contains(px, py)) return null;
  const point = view.toRoom(px, py);
  if (!allowOutside) {
    const room = roomOf();
    if (point.x < 0 || point.y < 0 || point.x > room.width_m || point.y > room.depth_m) return null;
  }
  return point;
}

function heatColor(t) {
  const stops = [
    [20, 40, 70],
    [30, 120, 160],
    [61, 220, 151],
    [240, 162, 2],
    [230, 70, 50],
  ];
  const x = Math.max(0, Math.min(0.999, t)) * (stops.length - 1);
  const i = Math.floor(x);
  const f = x - i;
  const a = stops[i];
  const b = stops[i + 1];
  return a.map((v, k) => Math.round(v + (b[k] - v) * f));
}

function draw() {
  resize();
  const room = roomOf();
  const view = map();
  const outer = view.rect();
  const [left, top] = view.toCanvas(0, room.depth_m);
  const [right, bottom] = view.toCanvas(room.width_m, 0);
  const fx = left;
  const fy = top;
  const w = right - left;
  const h = bottom - top;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#101820";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#121922";
  ctx.fillRect(outer.x, outer.y, outer.w, outer.h);

  roundRect(fx, fy, w, h, 12);
  ctx.fillStyle = "#182230";
  ctx.fill();

  ctx.save();
  ctx.beginPath();
  ctx.rect(fx, fy, w, h);
  ctx.clip();
  ctx.strokeStyle = "rgba(232,236,224,0.08)";
  ctx.lineWidth = 1;
  for (let m = 0; m <= room.width_m; m += 0.5) {
    const [x] = view.toCanvas(m, 0);
    ctx.beginPath();
    ctx.moveTo(x, fy);
    ctx.lineTo(x, fy + h);
    ctx.stroke();
  }
  for (let m = 0; m <= room.depth_m; m += 0.5) {
    const [, y] = view.toCanvas(0, m);
    ctx.beginPath();
    ctx.moveTo(fx, y);
    ctx.lineTo(fx + w, y);
    ctx.stroke();
  }
  ctx.restore();

  ctx.strokeStyle = "#9fb0a6";
  ctx.lineWidth = 3;
  ctx.strokeRect(fx + 1.5, fy + 1.5, w - 3, h - 3);
  ctx.fillStyle = "#8b978f";
  ctx.font = "14px Segoe UI";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillText(`${room.width_m} m wide`, fx + w / 2, fy + h + 8);
  ctx.save();
  ctx.translate(fx - 8, fy + h / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textBaseline = "bottom";
  ctx.fillText(`${room.depth_m} m deep`, 0, 0);
  ctx.restore();
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";

  const shelves = state.layout?.shelves || [];
  for (const shelf of shelves) {
    const [x, y] = view.toCanvas(shelf.x, shelf.y + shelf.h);
    const [x2, y2] = view.toCanvas(shelf.x + shelf.w, shelf.y);
    ctx.fillStyle = "rgba(108,176,255,0.12)";
    ctx.strokeStyle = "#6cb0ff";
    ctx.lineWidth = 2;
    ctx.fillRect(x, y, x2 - x, y2 - y);
    ctx.strokeRect(x, y, x2 - x, y2 - y);
    ctx.fillStyle = "#e7ece4";
    ctx.font = "14px Segoe UI";
    ctx.fillText(shelf.name, x + 8, y + 20);
  }

  const zones = state.layout?.zones || [];
  const zoneStats = new Map((state.live?.zone_view?.zones || []).map((row) => [row.id, row]));
  const maxDwell = Math.max(1, ...[...zoneStats.values()].map((row) => row.dwell_s || 0));
  for (const zone of zones) {
    const [x, y] = view.toCanvas(zone.x, zone.y + zone.h);
    const [x2, y2] = view.toCanvas(zone.x + zone.w, zone.y);
    const row = zoneStats.get(zone.id);
    const heat = state.heat ? (row?.dwell_s || 0) / maxDwell : 0;
    ctx.fillStyle = row?.inside ? "rgba(61,220,151,0.28)" : `rgba(240,162,2,${0.08 + heat * 0.45})`;
    ctx.strokeStyle = zone.kind === "walkway" ? "#c9a6ff" : "#f0a202";
    ctx.setLineDash(zone.kind === "walkway" ? [6, 4] : []);
    ctx.lineWidth = 2;
    ctx.fillRect(x, y, x2 - x, y2 - y);
    ctx.strokeRect(x, y, x2 - x, y2 - y);
    ctx.setLineDash([]);
    ctx.fillStyle = "#e7ece4";
    ctx.font = "14px Segoe UI";
    ctx.fillText(zone.name, x + 8, y + 20);
  }

  if (state.pending) {
    const shelf = state.pending;
    const [x, y] = view.toCanvas(shelf.x, shelf.y + shelf.h);
    const [x2, y2] = view.toCanvas(shelf.x + shelf.w, shelf.y);
    ctx.strokeStyle = "#f0a202";
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(x, y, x2 - x, y2 - y);
    ctx.setLineDash([]);
  }

  const trail = state.live?.mode === "zone" ? (state.live?.trail || []) : [];
  if (trail.length > 1) {
    ctx.beginPath();
    trail.forEach((point, i) => {
      const [x, y] = view.toCanvas(point[0], point[1]);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "rgba(61,220,151,0.45)";
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  const nodes = new Map((state.live?.nodes || []).map((node) => [node.id, node]));
  const router = state.layout?.router;
  if (router) {
    ctx.strokeStyle = "rgba(240,162,2,0.45)";
    ctx.lineWidth = 1;
    for (const sensor of state.layout?.sensors || []) {
      const [x1, y1] = view.toCanvas(router.x, router.y);
      const [x2, y2] = view.toCanvas(sensor.x, sensor.y);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
    const [rx, ry] = view.toCanvas(router.x, router.y);
    ctx.beginPath();
    ctx.arc(rx, ry, 14, 0, Math.PI * 2);
    ctx.fillStyle = "#3a2a10";
    ctx.fill();
    ctx.strokeStyle = "#f0a202";
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = "#f0a202";
    ctx.font = "700 12px Segoe UI";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("R", rx, ry);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }
  for (const sensor of state.layout?.sensors || []) {
    const [x, y] = view.toCanvas(sensor.x, sensor.y);
    const heard = nodes.get(sensor.id);
    ctx.beginPath();
    ctx.arc(x, y, 16, 0, Math.PI * 2);
    ctx.fillStyle = heard && !heard.stale ? "#16324f" : "#2a2024";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = heard && !heard.stale ? "#6cb0ff" : "#ff6b6b";
    ctx.stroke();
    ctx.fillStyle = "#e7ece4";
    ctx.font = "700 14px Segoe UI";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(sensor.id), x, y);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  const live = state.live;
  if (live?.mode === "zone" && live.present && live.x != null) {
    const [x, y] = view.toCanvas(live.x, live.y);
    const color = live.motion === "walking" ? "#f0a202" : "#3ddc97";
    ctx.beginPath();
    ctx.arc(x, y, 9, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  }

  requestAnimationFrame(draw);
}

function drawHeat(view, heat) {
  const { cols, rows, values } = heat;
  if (!values || values.length !== cols * rows) return;
  const img = document.createElement("canvas");
  img.width = cols;
  img.height = rows;
  const ictx = img.getContext("2d");
  const data = ictx.createImageData(cols, rows);
  for (let iy = 0; iy < rows; iy++) {
    for (let ix = 0; ix < cols; ix++) {
      const t = values[iy * cols + ix] / 255;
      const [r, g, b] = heatColor(t);
      const dst = ((rows - 1 - iy) * cols + ix) * 4;
      data.data[dst] = r;
      data.data[dst + 1] = g;
      data.data[dst + 2] = b;
      data.data[dst + 3] = Math.round(t * 170);
    }
  }
  ictx.putImageData(data, 0, 0);
  const { x, y, w, h } = view.rect();
  ctx.drawImage(img, x, y, w, h);
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function renderPanel() {
  const live = state.live;
  const badge = $("badge");
  if (!live || live.sensing !== "ok") {
    badge.textContent = "Offline";
    badge.className = "badge is-off";
  } else {
    badge.textContent = "Live";
    badge.className = "badge is-live";
  }

  const view = live?.zone_view || {};
  const zone = live?.zone?.name;
  if (live?.mode === "zone") {
    $("place").textContent = zone || (view.uncertain ? "Uncertain" : "Empty");
  } else if (!live?.present) {
    $("place").textContent = "Empty";
  } else {
    $("place").textContent = "Not taught";
  }
  const motion = live?.motion || "none";
  $("motion").textContent = motion === "none" ? "None" : motion === "walking" ? "Walking" : "Still";
  $("fix").textContent = live?.mode === "zone" ? "Zone model" : "Needs teaching";
  const evalResult = view.eval;
  if (evalResult?.measured && evalResult.ready) {
    $("note").textContent = `MEASURED ${(evalResult.accuracy * 100).toFixed(0)}% on a held-out session. Guessing the common zone would be ${(evalResult.majority * 100).toFixed(0)}%.`;
  } else if (evalResult?.reason) {
    $("note").textContent = evalResult.reason;
  } else {
    $("note").textContent = "Record each zone in session A and again in session B. The dot stays off until that check beats guessing.";
  }

  const stats = $("shelf-stats");
  stats.innerHTML = "";
  const zoneRows = view.zones || [];
  for (const row of zoneRows) {
    const li = document.createElement("li");
    if (row.inside) li.className = "is-in";
    li.innerHTML = `<strong>${escapeHtml(row.name)}</strong><span>${row.entries} visits · ${formatDwell(row.dwell_s)}</span>`;
    stats.appendChild(li);
  }
  if (!zoneRows.length) {
    stats.innerHTML = "<li><span>No zones yet. Draw them in Room.</span></li>";
  }
  $("route").textContent = (view.route || []).length ? `Route: ${view.route.join(" → ")}` : "";

  const nodes = $("node-list");
  nodes.innerHTML = "";
  const heard = live?.nodes || [];
  const without = view.rates?.without || {};
  const boosted = view.rates?.with || {};
  if (!heard.length) {
    nodes.innerHTML = "<li><span>Waiting for the boards.</span></li>";
  }
  for (const node of heard) {
    const li = document.createElement("li");
    const quiet = without[String(node.id)] != null ? `${without[String(node.id)]}/s` : "…";
    const loud = boosted[String(node.id)] != null ? `${boosted[String(node.id)]}/s` : "…";
    li.innerHTML = `<strong>Sensor ${node.id}</strong><span>${Math.round(node.rssi)} dBm · ${quiet} → ${loud}</span>`;
    nodes.appendChild(li);
  }

  const cap = view.teach;
  const status = $("capture-status");
  const places = teachPlaces();
  const placeName = places.find((item) => item.id === cap?.label)?.name;
  const label = cap?.label === "empty" ? "empty room" : (placeName || "the shelf");
  if (cap?.active && cap.phase === "go") {
    status.textContent = cap.label === "empty"
      ? `Leave the room. Recording starts in ${Math.ceil(cap.remaining_s)}s.`
      : `Go to ${label}. Recording starts in ${Math.ceil(cap.remaining_s)}s.`;
  } else if (cap?.active) {
    status.textContent = cap.label === "empty"
      ? `Stay out. ${Math.ceil(cap.remaining_s)}s left.`
      : `Move around at ${label}. ${Math.ceil(cap.remaining_s)}s left.`;
  } else if (cap?.error) status.textContent = cap.error;
  else if (cap && cap.error === null && cap.label) status.textContent = `Saved ${label} in session ${cap.session}.`;
  else status.textContent = "";

  const teach = $("teach-list");
  if (teach) {
    const counts = (view.recordings || {})[state.session] || {};
    const items = teachPlaces();
    const key = `${state.session}|${items.map((item) => item.id + item.name).join(",")}|${JSON.stringify(counts)}`;
    if (teach.dataset.key !== key) {
      teach.dataset.key = key;
      teach.innerHTML = "";
      for (const item of items) {
        const li = document.createElement("li");
        const saved = counts[item.id] || 0;
        li.innerHTML = `<span>${escapeHtml(item.name)}${saved ? ` · ${saved} windows` : ""}</span>`;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ghost";
        button.textContent = item.id === "empty" ? "Record empty" : "Record";
        button.addEventListener("pointerdown", (event) => {
          event.preventDefault();
          teachZone(item.id);
        });
        li.appendChild(button);
        teach.appendChild(li);
      }
    }
    teach.querySelectorAll("button").forEach((button) => {
      button.disabled = !!cap?.active;
    });
  }
  const evalNote = $("eval-note");
  const evalList = $("eval-list");
  if (evalNote) {
    evalNote.textContent = evalResult?.measured
      ? `MEASURED zone model ${(evalResult.accuracy * 100).toFixed(0)}%, guessing ${(evalResult.majority * 100).toFixed(0)}%, loudness only ${(evalResult.rssi * 100).toFixed(0)}%.`
      : (evalResult?.reason || "No held-out check yet.");
  }
  if (evalList) {
    evalList.innerHTML = "";
    const per = evalResult?.per_zone || {};
    for (const [name, score] of Object.entries(per)) {
      const li = document.createElement("li");
      li.innerHTML = `<span>${escapeHtml(name)}</span><span>${Math.round(score * 100)}%</span>`;
      evalList.appendChild(li);
    }
    for (const pair of evalResult?.confused || []) {
      const li = document.createElement("li");
      li.innerHTML = `<span>${escapeHtml(pair.actual)} read as ${escapeHtml(pair.guessed)}</span><span>${pair.count}</span>`;
      evalList.appendChild(li);
    }
  }

  $("hint").textContent = state.mode === "setup"
    ? "Place the boards inside the floor. Place the router outside the outline if it sits outside the room."
    : state.mode === "calibrate"
      ? "Press Record next to Shelf 1 or Shelf 2. Walk there, then move around in front of it."
      : "The dot sits in the middle of a zone, not on an exact spot.";
}

function teachPlaces() {
  const shelves = (state.layout?.shelves || []).map((shelf) => ({ id: shelf.id, name: shelf.name }));
  const zones = (state.layout?.zones || [])
    .filter((zone) => !shelves.some((shelf) => shelf.id === zone.id))
    .map((zone) => ({ id: zone.id, name: zone.name }));
  return [...shelves, ...zones, { id: "empty", name: "Empty room" }];
}

function formatDwell(seconds) {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

function fillSetup() {
  const room = roomOf();
  $("room-w").value = room.width_m;
  $("room-d").value = room.depth_m;
  const pick = $("sensor-pick");
  pick.innerHTML = "";
  for (const sensor of state.layout?.sensors || []) {
    const option = document.createElement("option");
    option.value = String(sensor.id);
    option.textContent = `Sensor ${sensor.id}`;
    pick.appendChild(option);
  }
  const list = $("shelf-edit");
  list.innerHTML = "";
  for (const shelf of state.layout?.shelves || []) {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${escapeHtml(shelf.name)}</strong>`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost";
    button.textContent = "Delete";
    button.addEventListener("click", () => {
      state.layout.shelves = state.layout.shelves.filter((item) => item.id !== shelf.id);
      saveLayout();
    });
    li.appendChild(button);
    list.appendChild(li);
  }
  const zones = $("zone-edit");
  zones.innerHTML = "";
  for (const zone of state.layout?.zones || []) {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${escapeHtml(zone.name)}</strong>`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost";
    button.textContent = "Delete";
    button.addEventListener("click", () => {
      state.layout.zones = state.layout.zones.filter((item) => item.id !== zone.id);
      saveLayout();
    });
    li.appendChild(button);
    zones.appendChild(li);
  }
}

async function saveLayout() {
  state.saving = true;
  try {
    state.layout = await api("/api/showroom/layout", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.layout),
    });
    showError("");
    fillSetup();
  } catch (err) {
    showError(err.message);
  } finally {
    state.saving = false;
  }
}

function setMode(mode) {
  state.mode = mode;
  state.tool = mode === "setup" ? "sensor" : state.tool;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("is-on", tab.dataset.mode === mode);
  });
  $("setup-card").hidden = mode !== "setup";
  $("cal-card").hidden = mode !== "calibrate";
  $("live-card").hidden = false;
  const aside = document.querySelector("aside");
  const lead = mode === "setup" ? $("setup-card") : mode === "calibrate" ? $("cal-card") : $("live-card");
  aside.insertBefore(lead, aside.firstChild);
  renderPanel();
}

async function teachZone(label) {
  const status = $("capture-status");
  if (status) status.textContent = "Starting…";
  try {
    await api("/api/showroom/teach", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, session: state.session }),
    });
    showError("");
  } catch (err) {
    if (status) status.textContent = "";
    showError(err.message);
  }
}

function setTool(name) {
  state.tool = name;
  for (const id of ["place-router", "draw-shelf", "draw-zone"]) {
    $(id).classList.toggle("is-on", name === id.replace("place-", "").replace("draw-", ""));
  }
  $("place-router").classList.toggle("is-on", name === "router");
  $("draw-shelf").classList.toggle("is-on", name === "shelf");
  $("draw-zone").classList.toggle("is-on", name === "zone");
}

canvas.addEventListener("pointerdown", (event) => {
  const point = pointerRoom(event, state.tool === "router");
  if (!point) return;
  if (state.mode !== "setup" || !state.layout) return;
  if (state.tool === "router") {
    state.layout.router = { x: point.x, y: point.y };
    saveLayout();
    return;
  }
  if (state.tool === "shelf" || state.tool === "zone") {
    state.drag = point;
    state.pendingKind = state.tool;
    canvas.setPointerCapture(event.pointerId);
    return;
  }
  const id = Number($("sensor-pick").value);
  const sensor = state.layout.sensors.find((item) => item.id === id);
  if (!sensor) return;
  sensor.x = point.x;
  sensor.y = point.y;
  saveLayout();
});

canvas.addEventListener("pointermove", (event) => {
  if (!state.drag) return;
  const point = pointerRoom(event);
  if (!point) return;
  const x = Math.min(state.drag.x, point.x);
  const y = Math.min(state.drag.y, point.y);
  state.pending = {
    x,
    y,
    w: Math.abs(point.x - state.drag.x),
    h: Math.abs(point.y - state.drag.y),
  };
});

canvas.addEventListener("pointerup", () => {
  if (!state.drag) return;
  state.drag = null;
  const pending = state.pending;
  if (!pending || pending.w < 0.25 || pending.h < 0.25) {
    state.pending = null;
    return;
  }
  $("new-shelf").hidden = false;
  const count = state.pendingKind === "zone" ? (state.layout.zones || []).length : (state.layout.shelves || []).length;
  $("shelf-name").value = `${state.pendingKind === "zone" ? "Zone" : "Shelf"} ${count + 1}`;
  $("shelf-name").focus();
});

$("place-router").addEventListener("click", () => setTool(state.tool === "router" ? "sensor" : "router"));
$("draw-shelf").addEventListener("click", () => setTool(state.tool === "shelf" ? "sensor" : "shelf"));
$("draw-zone").addEventListener("click", () => setTool(state.tool === "zone" ? "sensor" : "zone"));

$("shelf-add").addEventListener("click", () => {
  if (!state.pending) return;
  const name = $("shelf-name").value.trim();
  if (!name) return;
  if (state.pendingKind === "zone") {
    const id = `zone-${Date.now().toString(36)}`;
    state.layout.zones = state.layout.zones || [];
    state.layout.zones.push({ id, name, kind: $("zone-kind").value, ...state.pending });
  } else {
    const id = `shelf-${Date.now().toString(36)}`;
    state.layout.shelves.push({ id, name, ...state.pending });
  }
  state.pending = null;
  $("new-shelf").hidden = true;
  setTool("sensor");
  saveLayout();
});

$("shelf-cancel").addEventListener("click", () => {
  state.pending = null;
  $("new-shelf").hidden = true;
});

function onRoomInput() {
  if (!state.layout) return;
  state.layout.room.width_m = Number($("room-w").value);
  state.layout.room.depth_m = Number($("room-d").value);
  saveLayout();
}
$("room-w").addEventListener("change", onRoomInput);
$("room-d").addEventListener("change", onRoomInput);

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => setMode(tab.dataset.mode));
});

$("heat-toggle").addEventListener("change", (event) => {
  state.heat = event.target.checked;
});

$("reset-counts").addEventListener("click", async () => {
  try {
    await api("/api/showroom/session/reset", { method: "POST" });
  } catch (err) {
    showError(err.message);
  }
});

function setSession(session) {
  state.session = session;
  $("session-a").classList.toggle("is-on", session === "A");
  $("session-b").classList.toggle("is-on", session === "B");
  renderPanel();
}
$("session-a").addEventListener("click", () => setSession("A"));
$("session-b").addEventListener("click", () => setSession("B"));
$("run-eval").addEventListener("click", async () => {
  try {
    const result = await api("/api/showroom/eval", { method: "POST" });
    if (state.live) state.live.zone_view = { ...(state.live.zone_view || {}), eval: result };
    renderPanel();
  } catch (err) {
    showError(err.message);
  }
});

async function poll() {
  try {
    state.live = await api("/api/showroom/live");
    showError("");
  } catch (err) {
    showError(err.message);
  }
  renderPanel();
  setTimeout(poll, 200);
}

async function boot() {
  state.layout = await api("/api/showroom/layout");
  fillSetup();
  setMode("live");
  poll();
  requestAnimationFrame(draw);
}

window.addEventListener("resize", resize);
boot().catch((err) => showError(err.message));

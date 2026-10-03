// Interactive graph. Chart.js draws it; this file adds click-to-read points, drag-to-pan, wheel/pinch zoom
// and reset, and asks the worker for fresh points whenever the view changes, so the
// curve never runs out however far you move.

const MIN_WIDTH = 1e-6; // zoom limits (width of the view in x or y)
const MAX_WIDTH = 1e6;
const RESAMPLE_DELAY_MS = 80;

const plotEl = document.getElementById("plot");
const legendEl = document.getElementById("legend");
const canvas = document.getElementById("canvas");

let chart = null;
let homeView = null; // starting view, restored by Reset / double-click
let askForSamples = null; // (lo, hi) => request id; provided by app.js
let latestSampleId = null;
let fitYOnNextSamples = false;
let resampleTimer;
const pointers = new Map(); // pointerId -> {x, y}, for drag and pinch

// Click/tap to read a point: a press that moves less than this (in px) is a tap, not a drag.
const TAP_MAX_MOVE = 6;
let askForPoint = null; // (x) => request id; provided by app.js
let tap = null; // {id, x, y} of a press that hasn't moved (yet)
let marker = null; // {x, y, curve}; y is null where the function is undefined
// Point requests waiting for the worker: id -> {tapY} (pick the curve nearest the tap)
// or {curve} (keep this curve, while dragging the dot).
const pointRequests = new Map();

// Drag the dot along its curve: a press this close (px) to the dot grabs it instead of panning.
const DOT_GRAB = { mouse: 14, touch: 24 };
let scrub = null; // {id, x, y, moved} of the pointer pressing/dragging the dot

// Derivative tab: checkboxes choose which curves are drawn (only f′ by default, set in index.html).
const showF = document.getElementById("show-f");
const showFPrime = document.getElementById("show-df");
const legendSecant = document.getElementById("legend-secant");
const showSecant = document.getElementById("show-secant"); // the secant line has its own checkbox
let isDerivative = false;

// Important points, like Desmos: max/min, intercepts and f/f′ intersections, found by the worker
// for the region on screen: {curve: "f" | "df" | "both", kind, x, y}.
let importantPoints = [];
let secant = null; // [[a, f(a)], [b, f(b)]] on the derivative tab with From/To
const POINT_GRAB = { mouse: 12, touch: 22 }; // a click this close (px) to a point lands exactly on it
const SNAP_PX = 8; // the dragged dot snaps onto a point this close (px, sideways)
const MAX_DRAWN_POINTS = 60; // more than this on screen is clutter: don't draw them

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Datasets: 0 = f(x) curve, 1 = shaded area, 2 = f'(x) curve (derivative tab only).
const F = 0;
const AREA = 1;
const F_PRIME = 2;

// Neon glow on the curves, in each curve's own color. Chart.js has no built-in glow.
const glowPlugin = {
  id: "glow",
  beforeDatasetDraw(c, { index }) {
    if (index === AREA) return;
    c.ctx.save();
    c.ctx.shadowColor = c.data.datasets[index].borderColor;
    c.ctx.shadowBlur = 10;
  },
  afterDatasetDraw(c, { index }) {
    if (index !== AREA) c.ctx.restore();
  },
};

// 1.5707963 -> "1.5708", 0.99999999 -> "1", 1e-17 -> "0"
function fmt(v) {
  return Math.abs(v) < 1e-12 ? "0" : String(Number(v.toPrecision(5)));
}

const CURVE_OF = { f: F, df: F_PRIME };

function pointVisible(p) {
  const visible = visibleCurves();
  if (p.curve === "secant") return showSecant.checked;
  return p.curve === "both" ? visible.length === 2 : visible.includes(CURVE_OF[p.curve]);
}

// The important points you can click (plus the secant's two ends), on visible curves only.
function clickablePoints() {
  const ends = secant ? [{ curve: "secant", kind: "from", x: secant[0][0], y: secant[0][1] },
                         { curve: "secant", kind: "to", x: secant[1][0], y: secant[1][1] }] : [];
  return importantPoints.concat(ends).filter(pointVisible);
}

function pointColor(p) {
  return p.curve === "both" ? cssVar("--text") : chart.data.datasets[CURVE_OF[p.curve]].borderColor;
}

// Draws the secant line (dashed, through the From/To points, across the whole plot) and a small
// hollow dot at each important point.
const pointsPlugin = {
  id: "points",
  afterDatasetsDraw(c) {
    const { ctx, chartArea: area, scales } = c;
    ctx.save();
    ctx.beginPath();
    ctx.rect(area.left, area.top, area.width, area.height);
    ctx.clip();
    if (secant && showSecant.checked) {
      const [[x1, y1], [x2, y2]] = secant;
      const slope = (y2 - y1) / (x2 - x1);
      const at = (xv) => [scales.x.getPixelForValue(xv), scales.y.getPixelForValue(y1 + slope * (xv - x1))];
      const color = cssVar("--accent-3");
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(...at(scales.x.min));
      ctx.lineTo(...at(scales.x.max));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      for (const [xv, yv] of secant) {
        ctx.beginPath();
        ctx.arc(scales.x.getPixelForValue(xv), scales.y.getPixelForValue(yv), 4, 0, 2 * Math.PI);
        ctx.fill();
      }
    }
    const shown = importantPoints.filter(pointVisible).filter((p) =>
      p.x >= scales.x.min && p.x <= scales.x.max && p.y >= scales.y.min && p.y <= scales.y.max);
    if (shown.length <= MAX_DRAWN_POINTS) {
      ctx.lineWidth = 1.5;
      for (const p of shown) {
        ctx.strokeStyle = pointColor(p);
        ctx.fillStyle = cssVar("--bg");
        ctx.beginPath();
        ctx.arc(scales.x.getPixelForValue(p.x), scales.y.getPixelForValue(p.y), 3.5, 0, 2 * Math.PI);
        ctx.fill();
        ctx.stroke();
      }
    }
    ctx.restore();
  },
};

// Draws the clicked point: a glowing dot on the curve and an "(x, y)" label kept inside
// the plot. The label is drawn on the canvas, from numbers only.
const markerPlugin = {
  id: "marker",
  afterDatasetsDraw(c) {
    if (!marker) return;
    const { ctx, chartArea: area, scales } = c;
    const px = scales.x.getPixelForValue(marker.x);
    if (px < area.left || px > area.right) return; // panned out of view
    ctx.save();
    ctx.font = `12px ${cssVar("--mono")}`;
    let py, color, text;
    if (marker.y === null) {
      // f is undefined here: dashed line instead of a dot
      py = area.top;
      color = cssVar("--text-dim");
      text = `undefined at x = ${fmt(marker.x)}`;
      ctx.strokeStyle = color;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(px, area.top);
      ctx.lineTo(px, area.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
    } else {
      py = scales.y.getPixelForValue(marker.y);
      if (py < area.top || py > area.bottom) return ctx.restore();
      const crossing = marker.kind === "intersection"; // on both curves
      color = marker.color ?? (crossing ? cssVar("--text") : c.data.datasets[marker.curve].borderColor);
      const name = isDerivative && !crossing && !marker.color ? (marker.curve === F_PRIME ? "f′ " : "f ") : "";
      text = `${name}${marker.kind ? marker.kind + " " : ""}(${fmt(marker.x)}, ${fmt(marker.y)})`;
      ctx.fillStyle = color;
      ctx.shadowColor = color;
      ctx.shadowBlur = 12;
      ctx.beginPath();
      ctx.arc(px, py, 5, 0, 2 * Math.PI);
      ctx.fill();
      ctx.shadowBlur = 0;
    }
    const padX = 6, h = 20, w = ctx.measureText(text).width + 2 * padX;
    let lx = px + 10, ly = py - h - 8; // up and to the right of the point...
    if (lx + w > area.right) lx = px - 10 - w; // ...unless that leaves the plot
    if (ly < area.top) ly = py + 8;
    lx = Math.max(area.left, lx);
    ctx.fillStyle = "rgba(5, 7, 10, 0.88)";
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.roundRect(lx, ly, w, h, 4);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.textBaseline = "middle";
    ctx.fillText(text, lx + padX, ly + h / 2);
    ctx.restore();
  },
};

function axis([min, max]) {
  return {
    type: "linear",
    min,
    max,
    border: { display: false },
    // Brighter line at 0 so the x and y axes stand out from the grid.
    grid: { color: (ctx) => (ctx.tick?.value === 0 ? cssVar("--axis") : cssVar("--grid")) },
    // includeBounds: false -> only round tick values, not the view's raw edges (e.g. -0.785)
    ticks: { color: cssVar("--text-dim"), maxTicksLimit: 8, includeBounds: false, font: { family: cssVar("--mono"), size: 11 } },
  };
}

function showGraph(plot, requestSamples, requestPoint) {
  clearGraph();
  askForSamples = requestSamples;
  askForPoint = requestPoint;
  let xRange = [-10, 10];
  if (plot.a !== null) {
    const pad = 0.25 * (plot.b - plot.a || 1);
    xRange = [plot.a - pad, plot.b + pad];
  }
  homeView = { x: xRange, y: [-1, 1] };
  plotEl.hidden = false; // must be visible before Chart.js measures the canvas
  const line = { data: [], showLine: true, pointRadius: 0 };
  chart = new Chart(canvas, {
    type: "scatter",
    data: {
      datasets: [
        { ...line, borderWidth: 2, borderColor: cssVar("--accent") },
        { ...line, borderWidth: 0, fill: "origin", backgroundColor: cssVar("--accent-fill") },
        { ...line, borderWidth: 2, borderColor: cssVar("--accent-2") },
      ],
    },
    options: {
      animation: false,
      maintainAspectRatio: false,
      events: [], // we handle the pointer ourselves
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: axis(homeView.x), y: axis(homeView.y) },
    },
    plugins: [glowPlugin, pointsPlugin, markerPlugin],
  });
  isDerivative = Boolean(plot.derivative);
  legendEl.hidden = !isDerivative;
  secant = plot.secant ?? null;
  legendSecant.hidden = !secant; // its checkbox only appears with From/To (unticked by default)
  applyVisibility();
  fitYOnNextSamples = true;
  resample();
}

function applyVisibility() {
  chart.data.datasets[F].hidden = isDerivative && !showF.checked;
  chart.data.datasets[F_PRIME].hidden = !isDerivative || !showFPrime.checked;
  if (marker && !markerVisible()) marker = null;
}

// The dot stays only while what it sits on is shown (a secant end belongs to the secant).
function markerVisible() {
  const onSecantEnd = marker.kind === "from" || marker.kind === "to";
  return onSecantEnd ? showSecant.checked : visibleCurves().includes(marker.curve);
}

function visibleCurves() {
  return [F, F_PRIME].filter((i) => !chart.data.datasets[i].hidden);
}

function visibleData() {
  return visibleCurves().flatMap((i) => chart.data.datasets[i].data);
}

// The secant checkbox just shows/hides the dashed line and its ends (no re-fit needed).
showSecant.addEventListener("change", () => {
  if (!chart) return;
  if (marker && !markerVisible()) marker = null;
  chart.update("none");
});

// Ticking a box shows/hides that curve and re-fits y to what's now visible (x stays put).
for (const box of [showF, showFPrime]) {
  box.addEventListener("change", () => {
    if (!chart) return;
    applyVisibility();
    homeView.y = fitY(visibleData());
    setView(currentView().x, fitY(visibleData(), currentView().x));
  });
}

function clearGraph() {
  chart?.destroy();
  chart = null;
  latestSampleId = null;
  clearTimeout(resampleTimer);
  pointers.clear();
  plotEl.hidden = true;
  legendEl.hidden = true;
  marker = null;
  pointRequests.clear();
  tap = null;
  scrub = null;
  importantPoints = [];
  secant = null;
}

// The clickable point nearest to the pixel (px, py), within the grab radius, or null.
function pointNear(px, py, pointerType) {
  const radius = pointerType === "touch" ? POINT_GRAB.touch : POINT_GRAB.mouse;
  let best = null;
  let bestDistance = radius;
  for (const p of clickablePoints()) {
    const distance = Math.hypot(chart.scales.x.getPixelForValue(p.x) - px, chart.scales.y.getPixelForValue(p.y) - py);
    if (distance < bestDistance) [best, bestDistance] = [p, distance];
  }
  return best;
}

// Put the dot exactly on an important point (values come from the worker, no request needed).
function markPoint(p, curve = p.curve === "df" ? F_PRIME : F) {
  // The secant's ends ("from"/"to") are drawn in the secant's color.
  const color = p.curve === "secant" ? cssVar("--accent-3") : undefined;
  marker = { x: p.x, y: p.y, curve, kind: p.kind, color };
  pointRequests.clear(); // an older reply must not move the dot off the point
  chart.update("none");
}

// Ask the worker for the exact f(x) (and f'(x)) at x. `how` says which curve the answer goes on.
function requestPointAt(x, how) {
  const id = askForPoint(x);
  if (id !== null) pointRequests.set(id, how);
}

// A tap inside the plot: on an important point, land exactly on it; otherwise put the dot on
// the curve nearest to the tap.
function onTap(px, py, pointerType) {
  const area = chart.chartArea;
  if (px < area.left || px > area.right || py < area.top || py > area.bottom) return;
  const hit = pointNear(px, py, pointerType);
  if (hit) return markPoint(hit);
  requestPointAt(chart.scales.x.getValueForPixel(px), { tapY: py });
}

// The worker's answer. Replies arrive in order, so this one makes any older ones stale.
function applyPoint({ id, x, y, y2 }) {
  const how = pointRequests.get(id);
  if (!chart || !how) return;
  for (const older of pointRequests.keys()) if (older <= id) pointRequests.delete(older);
  const visible = visibleCurves();
  if (!visible.length) return;
  const values = { [F]: y, [F_PRIME]: y2 };
  let curve = how.curve;
  if (curve === undefined) {
    const defined = visible.filter((c) => values[c] != null);
    const distance = (c) => Math.abs(chart.scales.y.getPixelForValue(values[c]) - how.tapY);
    curve = defined.length ? defined.reduce((best, c) => (distance(c) < distance(best) ? c : best)) : visible[0];
  }
  marker = { x, y: values[curve] ?? null, curve };
  chart.update("none");
}

// Is the pixel (px, py) on the dot (or on the dashed "undefined" line)?
function onDot(px, py, pointerType) {
  if (!marker) return false;
  const radius = pointerType === "touch" ? DOT_GRAB.touch : DOT_GRAB.mouse;
  const mx = chart.scales.x.getPixelForValue(marker.x);
  if (marker.y === null) return Math.abs(px - mx) < radius;
  return Math.hypot(px - mx, py - chart.scales.y.getPixelForValue(marker.y)) < radius;
}

// Slide the dot to the x under the pointer: move it right away using the drawn curve,
// then ask the worker for the exact value.
function scrubTo(px) {
  const area = chart.chartArea;
  px = Math.min(Math.max(px, area.left), area.right);
  // Passing an important point on this curve: snap onto it, like Desmos.
  const curveName = marker.curve === F_PRIME ? "df" : "f";
  const onThisCurve = (p) => p.curve === curveName || p.curve === "both" || (p.curve === "secant" && curveName === "f");
  const snap = clickablePoints().find((p) => onThisCurve(p)
    && Math.abs(chart.scales.x.getPixelForValue(p.x) - px) < SNAP_PX);
  if (snap) return markPoint(snap, marker.curve);
  const x = chart.scales.x.getValueForPixel(px);
  marker = { x, y: interpolate(marker.curve, x), curve: marker.curve };
  chart.update("none");
  requestPointAt(x, { curve: marker.curve });
}

// y on the drawn curve at x (a straight line between the two nearest samples), or null.
function interpolate(curve, x) {
  const data = chart.data.datasets[curve].data;
  if (data.length < 2) return null;
  const step = data[1][0] - data[0][0];
  const i = Math.floor((x - data[0][0]) / step);
  if (i < 0 || i >= data.length - 1) return null;
  const [xa, ya] = data[i];
  const [xb, yb] = data[i + 1];
  if (ya === null || yb === null) return null;
  return ya + ((yb - ya) * (x - xa)) / (xb - xa);
}

function resample() {
  if (!chart) return;
  const { min, max } = chart.options.scales.x;
  const width = max - min;
  latestSampleId = askForSamples(min - width, max + width); // 3x the view: no empty edges while dragging
}

function scheduleResample() {
  clearTimeout(resampleTimer);
  resampleTimer = setTimeout(resample, RESAMPLE_DELAY_MS);
}

function applySamples({ id, curve, area, curve2 = [], points = [] }) {
  if (!chart || id !== latestSampleId) return; // stale reply
  importantPoints = points;
  chart.data.datasets[0].data = curve;
  chart.data.datasets[AREA].data = area;
  chart.data.datasets[2].data = curve2;
  if (fitYOnNextSamples) {
    fitYOnNextSamples = false;
    homeView.y = fitY(visibleData()); // fit the curves that are shown
    chart.options.scales.y.min = homeView.y[0];
    chart.options.scales.y.max = homeView.y[1];
  }
  chart.update("none");
}

// y-range for the visible points: 2nd-98th percentile (so a spike near an
// asymptote can't squash the plot), always including y = 0, plus 10% padding.
function fitY(curve, [xMin, xMax] = homeView.x) {
  const ys = curve.filter(([xv, yv]) => yv !== null && xv >= xMin && xv <= xMax).map(([, yv]) => yv);
  if (!ys.length) return [-1, 1];
  ys.sort((p, q) => p - q);
  const pick = (fraction) => ys[Math.round(fraction * (ys.length - 1))];
  const lo = Math.min(0, pick(0.02));
  const hi = Math.max(0, pick(0.98));
  const pad = 0.1 * (hi - lo || 1);
  return [lo - pad, hi + pad];
}

function setView(xRange, yRange) {
  const { x, y } = chart.options.scales;
  [x.min, x.max] = xRange;
  [y.min, y.max] = yRange;
  chart.update("none");
  scheduleResample();
}

function currentView() {
  const { x, y } = chart.options.scales;
  return { x: [x.min, x.max], y: [y.min, y.max] };
}

// Zoom both axes by `factor` (<1 zooms in) around the pixel (px, py).
function zoomAt(px, py, factor) {
  const view = currentView();
  const cx = chart.scales.x.getValueForPixel(px);
  const cy = chart.scales.y.getValueForPixel(py);
  const scale = ([min, max], c) => [c - (c - min) * factor, c + (max - c) * factor];
  const xRange = scale(view.x, cx);
  const yRange = scale(view.y, cy);
  const ok = ([min, max]) => max - min >= MIN_WIDTH && max - min <= MAX_WIDTH;
  if (ok(xRange) && ok(yRange)) setView(xRange, yRange);
}

// Move the view so the graph follows a drag of (dxPx, dyPx) pixels.
function panBy(dxPx, dyPx) {
  const view = currentView();
  const { width, height } = chart.chartArea;
  const dx = (dxPx * (view.x[1] - view.x[0])) / width;
  const dy = (dyPx * (view.y[1] - view.y[0])) / height; // pixels grow downward, y grows upward
  setView([view.x[0] - dx, view.x[1] - dx], [view.y[0] + dy, view.y[1] + dy]);
}

function resetView() {
  if (chart) setView(homeView.x, homeView.y);
}

canvas.addEventListener("wheel", (event) => {
  if (!chart) return;
  event.preventDefault(); // zoom the graph instead of scrolling the page
  const delta = event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY; // lines -> pixels (Firefox)
  zoomAt(event.offsetX, event.offsetY, Math.exp(delta * 0.0015));
}, { passive: false });

canvas.addEventListener("pointerdown", (event) => {
  if (!chart) return;
  canvas.setPointerCapture(event.pointerId);
  if (!pointers.size && onDot(event.offsetX, event.offsetY, event.pointerType)) {
    scrub = { id: event.pointerId, x: event.offsetX, y: event.offsetY, pointerType: event.pointerType, moved: false }; // grab the dot, don't pan
    tap = null;
    return;
  }
  pointers.set(event.pointerId, { x: event.offsetX, y: event.offsetY });
  // Could be a tap, unless it moves or a second finger joins (pinch).
  tap = pointers.size === 1
    ? { id: event.pointerId, x: event.offsetX, y: event.offsetY, pointerType: event.pointerType }
    : null;
});

canvas.addEventListener("pointermove", (event) => {
  if (!chart) return;
  if (scrub?.id === event.pointerId) {
    // Only start sliding once it really moves, so a click on the dot is still a click.
    scrub.moved ||= Math.hypot(event.offsetX - scrub.x, event.offsetY - scrub.y) > TAP_MAX_MOVE;
    if (scrub.moved) scrubTo(event.offsetX);
    return;
  }
  if (!pointers.size) { // just hovering: show that the dot can be dragged sideways
    canvas.style.cursor = onDot(event.offsetX, event.offsetY, event.pointerType) ? "ew-resize" : "";
  }
  if (!pointers.has(event.pointerId)) return;
  if (tap && Math.hypot(event.offsetX - tap.x, event.offsetY - tap.y) > TAP_MAX_MOVE) tap = null;
  const before = [...pointers.values()];
  pointers.set(event.pointerId, { x: event.offsetX, y: event.offsetY });
  const after = [...pointers.values()];
  if (after.length === 1) {
    panBy(after[0].x - before[0].x, after[0].y - before[0].y);
  } else if (after.length === 2) {
    // Pinch: follow the midpoint of the two fingers, zoom by how their distance changed.
    const mid = (p) => ({ x: (p[0].x + p[1].x) / 2, y: (p[0].y + p[1].y) / 2 });
    const dist = (p) => Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1;
    const m0 = mid(before);
    const m1 = mid(after);
    panBy(m1.x - m0.x, m1.y - m0.y);
    zoomAt(m1.x, m1.y, dist(before) / dist(after));
  }
});

canvas.addEventListener("pointerup", (event) => {
  if (scrub?.id === event.pointerId) { // let go of the dot
    if (!scrub.moved) onTap(scrub.x, scrub.y, scrub.pointerType); // pressed without moving: a normal click
    scrub = null;
    return;
  }
  pointers.delete(event.pointerId);
  if (chart && tap?.id === event.pointerId) onTap(tap.x, tap.y, tap.pointerType);
  tap = null;
});

canvas.addEventListener("pointercancel", (event) => {
  if (scrub?.id === event.pointerId) scrub = null;
  pointers.delete(event.pointerId);
  tap = null;
});

canvas.addEventListener("dblclick", resetView);
document.getElementById("reset").addEventListener("click", resetView);
